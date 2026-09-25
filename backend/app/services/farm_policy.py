"""Farm first-article + failure/quarantine policy (Phase 3).

Every run-lifecycle reaction to a terminal print status lives here so main.py
keeps a *single* hook call (``on_terminal``) and no farm logic leaks into the
345 KB monolith. Responsibilities:

- **First article**: a gated run prints one plate (``first_article=True``),
  holds (``first_article_state='awaiting_approval'``), and only materialises the
  rest of its plates once an operator approves. Approval can eject the part
  remotely (dispatch a part-present eject-only file) or trust that the operator
  physically removed it.
- **Retry**: a failed farm unit is re-queued up to ``retry_max_per_unit`` times,
  idempotently (exactly one retry per failure event, keyed by ``retry_of_id``).
- **Quarantine**: N consecutive terminal failures on one printer trip a
  DB-backed quarantine that excludes the printer from ALL dispatch.
- **Run pause**: when every printer a run can use is quarantined/offline the run
  is paused; a first-article reject also pauses it.
- **Run completion**: the last plate completing marks the batch ``completed``.

The eject GENERATION for a remote eject is a pure, tested helper
(``build_part_present_eject_file`` in ``services.eject.dispatch``); the FTPS
upload + MQTT ``project_file`` dispatch reuse the existing
``upload_file_async`` + ``printer_manager.start_print`` primitives — no
hand-rolled MQTT.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.core.websocket import broadcast_production_run_changed
from backend.app.models.farm_cycle_episode import KIND_EJECT
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.sku import SkuFile
from backend.app.schemas.settings import AppSettings
from backend.app.services import farm_correlation, pause_recovery, requeue
from backend.app.services.cycle_episodes import record_episode
from backend.app.services.dispatch_target import DispatchTarget
from backend.app.services.eject import geometry as eject_geometry, remote as eject_remote
from backend.app.services.hms_errors import format_hms_error_summary
from backend.app.services.notification_service import notification_service
from backend.app.services.plate_occupancy import FirstArticleEject, plate_occupancy
from backend.app.services.printer_manager import printer_manager
from backend.app.services.queue_builder import create_queue_items
from backend.app.services.sku_catalog import plate_units
from backend.app.utils.printer_models import is_bedslinger_model

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.services.terminal_outcome import TerminalOutcome

logger = logging.getLogger(__name__)

_TERMINAL_RUN_OUTCOMES = ("completed", "failed")

# The vendor's own pre-home Z primitive, from the stock H2S machine-start block's
# ``;===== avoid end stop =====`` sequence (``G380 S2 Z32 F1200`` / ``G380 S2 Z-12
# F1200``). ``G380 S2 Z<+d>`` is a relative Z move guarded by the BOTTOM end stop, so
# a bed already sitting on the stop truncates it to ~0 and a bed above the stop simply
# moves AWAY from the nozzle — either way it cannot touch a part. 32 mm is the vendor's
# figure, kept rather than re-derived: its only job is to reach the stop from wherever
# the firmware parked the bed after the pause.
VISION_HOLD_PROBE_MM = 32.0


def _hms_summary(hms_errors: list[dict] | None) -> str | None:
    """The printer's own account of a failure, as text a human can act on.

    ``hms_errors.format_hms_error_summary`` is THE renderer (it also writes
    ``PrintQueueItem.error_message``); this only adds the last-resort fall-back for a
    payload the catalogs cannot describe at all — the raw ``full_code``s, which are
    still greppable against the printer screen and the vendored tables. None means the
    terminal carried no codes whatsoever, which is itself worth saying in the page.
    """
    described = format_hms_error_summary(hms_errors)
    if described:
        return described
    if not hms_errors:
        return None
    raw = [str(e.get("full_code") or "").strip() for e in hms_errors]
    return ", ".join(code for code in raw if code) or None


# --------------------------------------------------------------------------- #
# Settings-backed policy defaults
# --------------------------------------------------------------------------- #
async def farm_policy_defaults(db: AsyncSession) -> tuple[int, int]:
    """Return ``(retry_max_per_unit, escalate_consecutive_failures)`` defaults.

    Read from the global farm settings, falling back to the schema defaults so a
    fresh install with no rows still resolves sane values.
    """
    from backend.app.api.routes.settings import get_setting

    retry_raw = await get_setting(db, "farm_retry_max_per_unit")
    escalate_raw = await get_setting(db, "farm_escalate_consecutive_failures")
    try:
        retry_max = int(retry_raw) if retry_raw is not None else 1
    except (TypeError, ValueError):
        retry_max = 1
    try:
        escalate = int(escalate_raw) if escalate_raw is not None else 2
    except (TypeError, ValueError):
        escalate = 2
    return retry_max, max(1, escalate)


# --------------------------------------------------------------------------- #
# Loading helpers
# --------------------------------------------------------------------------- #
async def _load_run(db: AsyncSession, run_id: int) -> PrintBatch:
    """Load a farm run (batch with sku_file_id) with items + sku, or 404."""
    result = await db.execute(
        select(PrintBatch)
        .where(PrintBatch.id == run_id)
        .options(
            selectinload(PrintBatch.queue_items),
            selectinload(PrintBatch.sku_file).selectinload(SkuFile.sku),
        )
    )
    run = result.scalar_one_or_none()
    if run is None or run.sku_file_id is None:
        raise HTTPException(status_code=404, detail="Production run not found")
    return run


def _sku_code(run: PrintBatch) -> str | None:
    return run.sku_file.sku.code if (run.sku_file and run.sku_file.sku) else None


# --------------------------------------------------------------------------- #
# Plan (deferred remaining plates) serialisation
# --------------------------------------------------------------------------- #
def build_first_article_plan(
    *,
    remaining: int,
    printer_ids: list[int] | None,
    target_model: str | None,
    base_fields: dict,
) -> str:
    """Serialise the not-yet-created plates so approval can materialise them."""
    return json.dumps(
        {
            "remaining": remaining,
            "printer_ids": printer_ids or None,
            "target_model": target_model,
            "base_fields": base_fields,
        }
    )


async def create_remaining_plates(db: AsyncSession, run: PrintBatch) -> int:
    """Materialise the deferred plates recorded in ``run.first_article_plan``.

    Returns the number of plate items created. Idempotent: consumes (clears) the
    plan so a duplicate approval can't double-create.
    """
    if not run.first_article_plan:
        return 0
    try:
        plan = json.loads(run.first_article_plan)
    except (TypeError, ValueError):
        run.first_article_plan = None
        await db.commit()
        return 0

    remaining = int(plan.get("remaining") or 0)
    base = dict(plan.get("base_fields") or {})

    if remaining <= 0:
        run.first_article_plan = None
        await db.commit()
        return 0

    # The plan's target is the run's target — the same POOL the first-article plate
    # was minted into, not a position in a round-robin. Every stored target column is
    # dropped from the template and re-written from the target itself.
    target = DispatchTarget.from_plan(plan)
    base.pop("printer_id", None)
    base.pop("target_model", None)
    base.pop("target_printer_ids", None)
    base.pop("first_article", None)
    # Approve-while-paused stages the materialised plates (manual_start=True) so
    # resume releases them together (R6, same class as R1); an active-run approval
    # dispatches immediately. Covers the _finalize_remote_eject path too.
    fields_common = {
        **base,
        "batch_id": run.id,
        "status": "pending",
        "first_article": False,
        "manual_start": run.status == "paused",
        **target.fields(),
    }

    await create_queue_items(db, count=remaining, printer_id=target.printer_id, fields=fields_common)

    run.first_article_plan = None
    await db.commit()
    return remaining


async def create_new_first_article(db: AsyncSession, run: PrintBatch) -> PrintQueueItem | None:
    """Create a fresh first-article item from the run's stored plan.

    Used by resume-after-reject to re-dispatch a new first article. The plan is
    left intact (still describes the remaining plates for the eventual approval).
    """
    if not run.first_article_plan:
        return None
    try:
        plan = json.loads(run.first_article_plan)
    except (TypeError, ValueError):
        return None
    base = dict(plan.get("base_fields") or {})
    target = DispatchTarget.from_plan(plan)
    base.pop("printer_id", None)
    base.pop("target_model", None)
    base.pop("target_printer_ids", None)
    base.pop("first_article", None)
    fields = {
        **base,
        "batch_id": run.id,
        "status": "pending",
        "first_article": True,
        **target.fields(),
    }
    items = await create_queue_items(db, count=1, printer_id=target.printer_id, fields=fields)
    # A fresh first article supersedes the previous rejection.
    run.first_article_reject_reason = None
    return items[0] if items else None


# --------------------------------------------------------------------------- #
# Terminal-status hook (the single entry point called from main.py)
# --------------------------------------------------------------------------- #
async def on_terminal(
    db: AsyncSession,
    printer_id: int | None,
    queue_item_id: int | None,
    final_status: str,
    archive_data: dict | None = None,
    completed_subtask_id: str | None = None,
    completed_subtask_name: str | None = None,
    hms_errors: list[dict] | None = None,
    outcome: TerminalOutcome | None = None,
) -> None:
    """React to a terminal print status. Non-farm prints are a no-op.

    Called ONCE per terminal from ``main.on_print_complete``, as its own spawned task
    with its own session — independent of the notification, which used to host it (and
    whose failure could take the policy down with it). Wraps each sub-action so a
    notification failure can never abort a committed state change.

    ``outcome`` is the terminal's ONE classification (``terminal_outcome``), built before
    any consumer mutated state; the disposition reads its VERDICT and the fault kinds it
    captured, never the store after the terminal's own closers ran. ``final_status`` is
    its ``recorded_status``. The other callers — the scheduler's dispatch-time failure
    and the monitor's downtime eject reconciles — have no printer classification to hand
    over and pass none: no verdict, no captured fault, which is exactly what those
    terminals are.

    ``completed_subtask_id`` / ``completed_subtask_name`` are the terminal payload's
    subtask id + name, used to confirm that a terminal is really the server-dispatched
    eject job Bambuddy started before consuming the pending eject — a foreign terminal
    must not finalise / clear someone else's plate (Phase 1). After a restart the id
    check turns lenient (the client's ``last_dispatch_subtask_id`` is gone), so the
    name check re-establishes positive identity for a HYDRATED pending (W1/R2).

    ``hms_errors`` is the terminal payload's live HMS list (the same one
    ``main.on_print_complete`` writes ``error_message`` from). It is the printer's own
    account of WHY a job ended, and the rejected-eject branch below pages with it —
    "unable to parse the file" is the difference between a sweep that failed and a file
    the printer never read.
    """
    try:
        # 1. Server-dispatched eject job terminal (production OR first-article). The
        #    eject is started via start_print (no queue item), so the printer echoes
        #    the submission id back; a registered PendingEject on this printer whose
        #    echo matches means THIS terminal is that eject ending. Accept "completed"
        #    AND the failed-at-EOF cosmetic — matching the echo IS the signal the job
        #    ended (a standalone eject file can end FAILED at EOF even after a clean
        #    sweep). A positive echo MISMATCH = foreign; leave the pending eject for
        #    the real terminal and fall through.
        if printer_id is not None and plate_occupancy.eject_identity(printer_id) is not None:
            if not eject_remote.matches_pending_eject(
                printer_id, completed_subtask_id, subtask_name=completed_subtask_name
            ):
                logger.info(
                    "farm_policy: printer %s terminal subtask %r/%r != dispatched eject — foreign, pending eject kept",
                    printer_id,
                    completed_subtask_id,
                    completed_subtask_name,
                )
                # fall through to item-based policy
            else:
                # Read the record BEFORE resolving it — ``resolve_eject`` retires the
                # pending, and every log line and branch below is keyed off what it
                # said. The resolution itself (and the gate that rides on it) is the
                # authority's: ``completed`` clears the plate, ``unverified`` leaves it
                # occupied under an escalation-only hold.
                pending = plate_occupancy.pending_eject_view(printer_id)
                if pending is None:  # pragma: no cover — the identity check just saw it
                    return
                logger.info(
                    "farm_policy: %s eject job on printer %s ended '%s'", pending.purpose, printer_id, final_status
                )
                # Runtime series for EVERY eject purpose (2026-07-31). The gouged-plate
                # incident's timeline had to be rebuilt by hand from print history
                # because nothing ever recorded how long a sweep took; one INFO line
                # per eject makes "is 179 s unusual?" answerable from the logs alone.
                # ONE "now" for both the log line and the ledger row, so the two can
                # never disagree about when this sweep ended.
                eject_ended_at = datetime.now(timezone.utc)
                actual_s = (
                    (eject_ended_at - pending.started_at).total_seconds() if pending.started_at is not None else None
                )
                if actual_s is not None:
                    # ``start_z`` rides the line because the expectation is a function of
                    # it: seeded, the estimate MEASURED the block's first Z move; unseeded,
                    # it bounded it. Two populations of one instrument — a runtime series
                    # that does not say which is which cannot be read.
                    logger.info(
                        "farm_policy: %s eject on printer %s ran %.0fs (expected %s, start_z=%s)",
                        pending.purpose,
                        printer_id,
                        actual_s,
                        f"{pending.expected_runtime_s:.0f}s" if pending.expected_runtime_s is not None else "n/a",
                        f"{pending.start_z:g}" if pending.start_z is not None else "unseeded",
                    )
                    # The same measurement, kept. Written for EVERY purpose and ahead
                    # of the watchdog/never-started branches below: a sweep the
                    # watchdog stopped is still a measured episode, and its ``outcome``
                    # — the printer's own terminal word — is what says it did not
                    # complete.
                    #
                    # It rides THIS session, inside its own savepoint: the row is then
                    # atomic with the terminal's own writes, opens no second connection
                    # (which on SQLite would queue behind this very transaction on every
                    # eject) and leaves nothing running after the handler returns.
                    await record_episode(
                        db,
                        printer_id,
                        KIND_EJECT,
                        started_at=pending.started_at,
                        ended_at=eject_ended_at,
                        expected_s=pending.expected_runtime_s,
                        outcome=final_status,
                        variant=pending.purpose,
                    )
                    # This branch had no DB write of its own until the episode, and so
                    # no commit: every one of its four callers (main's two notification
                    # lanes, the monitor's two downtime reconciles) hands over a session
                    # inside `async with async_session() as db:` and closes it without
                    # committing, and the plate authority persists through its own
                    # injected writer. The first writer on a path owns establishing the
                    # commit — the same thing this handler already does for
                    # ``waiting_reason`` further down. Nothing else is pending here: the
                    # eject branch is the handler's first act and the notification pass
                    # ahead of it never touches this session, so this publishes exactly
                    # the row above.
                    await db.commit()
                if pending.runtime_exceeded_at is not None:
                    # The in-flight watchdog already stopped this job and paged the
                    # operator. Whatever status the printer echoed — cancelled/failed
                    # from our stop, or completed when the stop lost the race or an
                    # MQTT drop let the file run out — the sweep is UNVERIFIED: never
                    # release the gate, never finalise an FA approval, never quarantine
                    # (an obstruction suspicion is not a hardware fault; the held gate
                    # alone parks the printer). Purpose-independent: a stalling machine
                    # deserves stopping no matter who asked for the sweep. No
                    # notification here — the watchdog owns paging, so honouring the
                    # mark can never double-alert. ``unverified`` is what keeps the
                    # plate occupied AND re-attaches the escalation-only hold: the
                    # policy driver arms it off the plate the resolve leaves behind,
                    # which is why nothing is armed here by hand.
                    logger.warning(
                        "farm_policy: %s eject on printer %s ended '%s' after the in-flight watchdog stopped it "
                        "(ran %s vs expected %s) — sweep UNVERIFIED, plate stays gated for a human",
                        pending.purpose,
                        printer_id,
                        final_status,
                        f"{actual_s:.0f}s" if actual_s is not None else "n/a",
                        f"{pending.expected_runtime_s:.0f}s" if pending.expected_runtime_s is not None else "n/a",
                    )
                    plate_occupancy.resolve_eject(printer_id, "unverified")
                    return
                if pending.started_at is None and not pending.hydrated and final_status != "completed":
                    # The printer REFUSED the file at setup — it never started the job, so
                    # nothing moved and nothing swept. ``started_at`` is stamped by the
                    # PRINT START echo, so "no start + a non-completed terminal" is exactly
                    # that shape: PREPARE → FAILED, the 005-H2S 2026-09-17 incident (HMS
                    # 0500_4003 "unable to parse the file" — the eject was built from the
                    # wrong donor and the commanded plate member was absent).
                    #
                    # A HYDRATED pending is excluded because there ``started_at`` is None BY
                    # CONSTRUCTION (the durable mirror is one timestamp column, not the built
                    # artifact) — it means "the farm restarted mid-sweep and cannot say", not
                    # "never started". Same pairing as ``expire_eject_start``, for the same
                    # reason: a rule keyed on the stamp alone fires on every restart.
                    #
                    # It is a FILE fault, not a machine fault: quarantining the printer or
                    # pausing the run would park healthy hardware over a bad build, and the
                    # next eject of a repaired file has nothing to recover from. So: keep
                    # the gate (the part is still on the plate), page a human with the
                    # printer's own codes, and leave the printer dispatchable. Deliberately
                    # ahead of the purpose fork — production, manual and FA all mean the
                    # same thing here, and none of their reactions is owed.
                    codes = _hms_summary(hms_errors) or "no HMS code reported"
                    logger.warning(
                        "farm_policy: %s eject on printer %s ended %r before the printer ever started it — "
                        "the printer rejected the eject file (%s); plate stays gated, no quarantine",
                        pending.purpose,
                        printer_id,
                        final_status,
                        codes,
                    )
                    plate_occupancy.resolve_eject(printer_id, "unverified")
                    try:
                        from backend.app.services.eject.monitor import notify_plate_not_empty

                        await notify_plate_not_empty(
                            printer_id,
                            source_detail=(
                                f"the printer rejected the eject file before starting it ({codes}) — nothing was "
                                "swept; remove the part by hand and Mark plate cleared, or eject again once the "
                                "cause is fixed"
                            ),
                        )
                    except Exception:  # noqa: BLE001 — a page must never abort the resolve above
                        logger.warning(
                            "farm_policy: rejected-eject page failed for printer %s", printer_id, exc_info=True
                        )
                    return
                if pending.purpose == "fa":
                    if final_status == "completed":
                        plate_occupancy.resolve_eject(printer_id, "completed")
                        if pending.run_id is not None:
                            await _finalize_remote_eject(db, pending.run_id, printer_id)
                    else:
                        plate_occupancy.resolve_eject(printer_id, "unverified")
                        # Sweep unverified: do NOT approve or materialise plates. The
                        # gate stays set and the run stays awaiting_approval (a
                        # re-approve after recovery re-dispatches). Quarantine mirrors
                        # the production branch — a failed sweep needs eyes either way.
                        await quarantine_printer(
                            db,
                            printer_id,
                            reason=(
                                f"First-article eject job ended '{final_status}' — sweep unverified, "
                                "plate kept gated; recover, then re-approve"
                            ),
                            failure_count=1,
                        )
                    return
                if pending.purpose == "manual":
                    # Foreign-plate manual eject (operator "Eject now" on a plate the
                    # farm did not dispatch). It owns no queue item / run, so there is
                    # nothing to retry, quarantine, or pause — completed clears the
                    # gate exactly like production; any other terminal keeps the gate
                    # raised (fail-closed) and only WARNs.
                    if final_status == "completed":
                        plate_occupancy.resolve_eject(printer_id, "completed")
                        logger.info(
                            "farm_policy: manual eject on printer %s completed — plate-clear gate released", printer_id
                        )
                    else:
                        plate_occupancy.resolve_eject(printer_id, "unverified")
                        logger.warning(
                            "farm_policy: manual eject on printer %s ended '%s' — sweep unverified, "
                            "plate kept gated (no quarantine)",
                            printer_id,
                            final_status,
                        )
                    return
                # production eject
                if final_status == "completed":
                    plate_occupancy.resolve_eject(printer_id, "completed")
                    logger.info(
                        "farm_policy: production eject on printer %s completed — plate-clear gate released", printer_id
                    )
                    # Production ONLY (operator ruling): a manual eject has an operator
                    # standing at the machine, so its plate must stay at working height.
                    await _maybe_idle_deep_park(db, printer_id)
                else:
                    # Sweep unverified: KEEP the gate, quarantine the printer, and
                    # pause the unit's run if it now has no available printers.
                    plate_occupancy.resolve_eject(printer_id, "unverified")
                    await quarantine_printer(
                        db,
                        printer_id,
                        reason=f"Eject job ended '{final_status}' — sweep unverified, plate kept gated",
                        failure_count=1,
                    )
                    if pending.run_id is not None:
                        batch = await db.get(PrintBatch, pending.run_id)
                        if batch is not None:
                            await _maybe_pause_run_no_printers(db, batch)
                return

        # 2. A REFUSED plate: the printer's own plate check paused this job and it ended
        #    without printing. The plate authority already holds the plate for a human
        #    (the terminal's one plate call), so what is owed here is the one motion — the
        #    bed lifted off the plate-release aid, where the firmware parked it for the
        #    whole pause — farm unit or foreign print alike; a farm unit is then requeued
        #    below.
        if (
            printer_id is not None
            and outcome is not None
            and outcome.verdict == farm_correlation.STOP_VERDICT_PLATE_REFUSED
        ):
            await _maybe_lift_held_bed(db, printer_id)

        # 3. Item-based policy.
        if queue_item_id is None:
            return
        item = await db.get(PrintQueueItem, queue_item_id)
        if item is None or item.batch_id is None:
            return
        batch = await db.get(PrintBatch, item.batch_id)
        if batch is None or batch.sku_file_id is None:
            return  # non-farm batch — leave it alone

        # Terminal-transition hygiene (W4b): a farm unit reaching a terminal status
        # must not keep a stale hold token. The 2026-07-20 incident left completed/
        # cancelled rows flagged spool_jam_recovery_failed / printer_offline_stalled /
        # print_paused_stalled forever. This hook is the single reaction point for
        # EVERY farm terminal that flows through main.on_print_complete (archive +
        # no-archive paths) and the scheduler dispatch-failure path
        # (print_scheduler._fail_queue_item), so clearing here covers them all. Only
        # touches this exact unit — a still-printing sibling keeps its own reason.
        if item.waiting_reason is not None:
            item.waiting_reason = None
            await db.commit()

        # The DISPOSITION is decided ONCE, from why the print ended, and AHEAD of the
        # ``final_status`` fork below — the terminal's classification says it, not the
        # status string: a dispatch-time failure arrives here as ``failed`` with no
        # verdict at all, and a refused first article is recorded ``cancelled`` precisely
        # so it never reaches ``_on_item_failed``. ``completed`` is excluded: a stop that
        # lost the race to a finishing print produced a part, and requeuing it would
        # print an extra plate.
        if final_status != "completed" and _requeues_gracefully(outcome):
            await on_farm_requeue(db, batch, item, outcome=outcome)
            return

        if final_status == "completed":
            await _on_item_completed(db, batch, item, archive_data)
        elif final_status == "failed":
            await _on_item_failed(db, batch, item)
        elif final_status == "cancelled":
            # A farm unit that ended WITHOUT producing its plate and without failing.
            # NOT a failure: no retry, no quarantine contribution — a visible hold +
            # notification, and RESUME tops the deficit back up (Phase 3.1).
            #
            # An attributed stop (`operator_ui` / `operator_screen`) is the ordinary
            # case. A `cancelled` with NO ``stop_source`` takes the SAME disposition
            # since 2026-09-19, and that is the point: it used to be a deliberate
            # no-op, which meant an interruption nobody could attribute — the downtime
            # reconcile's IDLE branch was the live one — left the run ACTIVE, one plate
            # short, with nothing on any surface to say so. An unknown outcome is not a
            # completed one; the honest reading is "a human has to look", which is
            # exactly what this disposition arranges. (The reconcile now also STAMPS
            # ``reconcile_unknown`` so the lineage says WHY, but the fork no longer
            # depends on the stamp having been written.)
            await on_operator_stop(db, batch, item)
    except Exception:  # noqa: BLE001 — policy must never crash the callback chain
        logger.exception("farm_policy.on_terminal failed for item=%s status=%s", queue_item_id, final_status)


# --------------------------------------------------------------------------- #
# Idle deep-park
# --------------------------------------------------------------------------- #
# Fallbacks come from the settings schema itself, so the default lives in ONE place
# (same pattern as services/eject/monitor.py's cooldown fallbacks).
_IDLE_PARK_ENABLED_DEFAULT: bool = bool(AppSettings.model_fields["farm_idle_park_enabled"].default)
_IDLE_PARK_PERCENT_DEFAULT: int = int(AppSettings.model_fields["farm_idle_park_percent"].default)
_IDLE_PARK_PERCENT_MIN = 10
_IDLE_PARK_PERCENT_MAX = 95


async def _maybe_idle_deep_park(db: AsyncSession, printer_id: int) -> None:
    """Lower the bed of an idle printer after a clean PRODUCTION eject.

    A printer whose plate was just swept and that has nothing slated parks its bed
    deep (a configured percentage of the model's commandable Z travel), so the idle
    fleet sits in a consistent, reachable position instead of at whatever height the
    last sweep left. Server-side G-code over the MQTT ``gcode_line`` lane — it is
    deliberately NOT part of the eject file, because the eject file's runtime is
    watchdogged and a park appended to it would read as a stalled sweep.

    Purely cosmetic, therefore fail-quiet by construction: every failure path is a
    log line. Nothing here escalates, retries, quarantines, or raises into the
    terminal chain — a printer that never parks is a printer that is simply parked
    where the eject left it.

    Skipped when: the setting is off; farm work (bound, or pool work that can land
    here) is slated for the printer; the model is a bedslinger (the gantry carries
    Z — there is no bed-on-Z travel to park into, the same physics that refuses the
    eject bed-drop); or the model has no geometry row / no ``z_travel_mm`` (nothing
    to take a percentage of).

    Callers: the production-eject completed branch of :func:`on_terminal` only. The
    watchdog-killed branch returns before it (a stalled sweep must never command
    more motion) and the manual branch never calls it (an operator is present).

    Frame note (2026-09-04): on a model whose Z re-reference ladder has run
    (``printer_model_geometry.z_reference_validated``), the eject block that precedes
    this park opens by DECLARING its own frame (``G92 Z<z_travel_mm>`` after the
    guarded drive onto the bottom stop), so this absolute ``G1 Z`` is executed in the
    eject's declared frame rather than the firmware's boot frame. That is the intended
    reading — the declared frame is the true one — and it is stated here because an
    absolute Z is only ever as good as the frame it runs in.
    """
    try:
        from backend.app.api.routes.settings import get_setting

        raw_enabled = await get_setting(db, "farm_idle_park_enabled")
        enabled = _IDLE_PARK_ENABLED_DEFAULT if raw_enabled is None else raw_enabled.strip().lower() == "true"
        if not enabled:
            return

        printer = await db.get(Printer, printer_id)
        if printer is None:
            return
        # Work bound to THIS printer, or POOL work the scheduler could land here —
        # either way the bed is about to be used.
        if await farm_correlation.farm_work_slated_for(db, printer_id=printer_id, printer_model=printer.model):
            return

        if is_bedslinger_model(printer.model):
            return

        geometry = await eject_geometry.get_geometry(db, printer.model)
        if geometry is None or geometry.z_travel_mm is None:
            logger.debug(
                "farm_policy: idle deep-park skipped on printer %s — no z_travel_mm for model %r",
                printer_id,
                printer.model,
            )
            return

        raw_percent = await get_setting(db, "farm_idle_park_percent")
        try:
            percent = int(raw_percent) if raw_percent is not None else _IDLE_PARK_PERCENT_DEFAULT
        except (TypeError, ValueError):
            percent = _IDLE_PARK_PERCENT_DEFAULT
        percent = max(_IDLE_PARK_PERCENT_MIN, min(_IDLE_PARK_PERCENT_MAX, percent))
        park_z = geometry.z_travel_mm * percent / 100.0

        client = printer_manager.get_client(printer_id)
        if client is None:
            logger.warning("farm_policy: idle deep-park skipped on printer %s — no MQTT client", printer_id)
            return
        # M400 BEFORE M18 is deliberate: drain the motion queue so the steppers are
        # only released once the descent has actually finished. Cutting them
        # mid-descent drops the bed the rest of the way under its own weight.
        ok = client.send_gcode(f"M17\nG90\nG1 Z{park_z:.1f} F900\nM400\nM18")
        if ok:
            logger.info(
                "farm_policy: idle deep-park sent on printer %s (Z%.1f = %d%% of z_travel %.0f)",
                printer_id,
                park_z,
                percent,
                geometry.z_travel_mm,
            )
        else:
            logger.warning("farm_policy: idle deep-park command refused on printer %s", printer_id)
    except Exception:  # noqa: BLE001 — cosmetic lane: never raises into the terminal chain
        logger.warning("farm_policy: idle deep-park failed on printer %s", printer_id, exc_info=True)


async def _on_item_completed(
    db: AsyncSession, batch: PrintBatch, item: PrintQueueItem, archive_data: dict | None
) -> None:
    if item.first_article and batch.first_article_state == "pending_print":
        batch.first_article_state = "awaiting_approval"
        await db.commit()
        broadcast_production_run_changed(batch.id)
        await _notify_first_article_pending(db, batch, item, archive_data)
        return
    await _maybe_complete_run(db, batch)


async def _on_item_failed(db: AsyncSession, batch: PrintBatch, item: PrintQueueItem) -> None:
    # A terminal run (aborted/completed) still counts this failure toward printer
    # health — quarantine is independent of run intent — but must NOT mint a
    # dispatchable retry (R1/R2: a cancelled run would silently print an extra
    # plate) nor evaluate the run-pause helpers.
    if batch.status in ("cancelled", "completed"):
        await maybe_quarantine_printer(db, batch, item)
        return

    retry_max = batch.retry_max_per_unit if batch.retry_max_per_unit is not None else 1
    # The cap counts the plate's GENUINE failures — the FAILED ancestors of its chain,
    # not ``retry_count`` (the generation index every lineage-only requeue advances).
    if await requeue.failed_ancestor_count(db, item) < retry_max:
        # A paused run keeps re-queuing failed units, but STAGED (manual_start=True)
        # so the retry can't dispatch while paused; resume's manual_start sweep
        # releases it (R1). An active run's retry dispatches as today.
        await requeue.requeue_attempt(item.id, cause="failed", stage_manual=batch.status == "paused")
    await maybe_quarantine_printer(db, batch, item)
    await _maybe_pause_run_no_printers(db, batch)
    await _maybe_pause_run_exhausted(db, batch)


async def on_operator_stop(db: AsyncSession, batch: PrintBatch, item: PrintQueueItem) -> None:
    """A farm unit ended without its plate and without failing (Phase 3.1).

    Called from :func:`on_terminal` for EVERY farm item that lands terminal
    ``cancelled`` — the operator's stop (``stop_source`` set) and, since 2026-09-19,
    an outcome the farm could not learn at all (``reconcile_unknown``, or no stamp).
    The name is the common case, not the whole set: what unites them is that the unit
    produced no part and nothing failed, so the run must HOLD for a human rather than
    count it either way. Deliberately does the OPPOSITE of a failure:

    - NO auto-retry (the operator chose to stop this unit);
    - NOT counted toward quarantine — ``cancelled`` is already outside
      ``_TERMINAL_RUN_OUTCOMES`` (kept that way), so ``recent_terminal_farm_items``
      never sees it;
    - the plate-clear gate is left exactly as the deposit path set it (the part is
      on the plate for the operator to clear);
    - the run STAYS ``active`` but records ``pause_reason='operator_stop'`` as a
      visible hold — RESUME clears it and tops the run back up (``top_up_run``);
    - fires the run-scoped ``on_run_unit_stopped`` notification. The generic
      upstream ``on_print_stopped`` may also fire independently — their templates
      don't duplicate content.
    """
    batch.pause_reason = "operator_stop"
    await db.commit()
    broadcast_production_run_changed(batch.id)

    printer_name = "Unknown"
    if item.printer_id is not None:
        printer = await db.get(Printer, item.printer_id)
        if printer is not None:
            printer_name = printer.name
    run = await _load_run(db, batch.id)
    await notification_service.on_run_unit_stopped(item.printer_id, printer_name, run.name, db)
    logger.info(
        "farm_policy: unit %s ended without a plate (stop_source=%s) on run %s — "
        "no retry, no quarantine, run held (active)",
        item.id,
        item.stop_source or "unattributed",
        batch.id,
    )


# --------------------------------------------------------------------------- #
# Graceful requeue — the third terminal disposition
# --------------------------------------------------------------------------- #
def _requeues_gracefully(outcome: TerminalOutcome | None) -> bool:
    """Does this terminal mean "do this plate again" rather than "it failed/was cancelled"?

    Read off the terminal's ONE classification — its verdict and the fault kinds it
    captured BEFORE any closer ran — never off the store afterwards. Two routes, and only
    two:

    * the printer REFUSED the plate (``plate_refused``): its own plate check paused the
      job, and the job ended without printing. Nothing was consumed and nothing failed.
    * an OPERATOR stopped a print while the printer held an open EQUIPMENT FAULT
      (runout, jam, physical, power loss, …). The machine was already holding and the
      human stopping it is finishing what the hold started — the plate still has to be
      made. This is the 2026-09-11 ruling, and it depends on the capture: the terminal's
      own closer ends a ``wire`` hold (runout / jam / power loss) at this very terminal,
      so a store read here would find nothing, and in production it never fired. A plain
      operator stop with NO fault is unchanged: it means "cancel this work", and keeps
      :func:`on_operator_stop`'s semantics (cancelled, the run holds, RESUME tops the
      deficit back up).

    **A HOLD IS NOT A FAULT**: ``faults_open`` is the ``FAULT_KINDS`` subset, so an
    operator who stops a print on a printer whose only hold is maintenance mode keeps
    the ordinary operator-stop disposition — nothing is broken and nothing was
    interrupted by the equipment.
    """
    if outcome is None:
        return False
    if outcome.verdict == farm_correlation.STOP_VERDICT_PLATE_REFUSED:
        return True
    return outcome.operator_stopped and bool(outcome.faults_open)


async def on_farm_requeue(
    db: AsyncSession, batch: PrintBatch, item: PrintQueueItem, *, outcome: TerminalOutcome | None
) -> None:
    """The plate was REFUSED, not failed: queue it again with the same settings.

    The third disposition beside completed / failed / operator-stop, and deliberately
    unlike all three:

    - **lineage only.** The requeue carries ``retry_of_id`` / ``retry_count`` so the
      run-detail chain reads as one plate, but it never consumes
      ``farm_retry_max_per_unit``: the cap counts the FAILED ancestors of the chain
      (``requeue.failed_ancestor_count``), and this row is ``cancelled`` — a refused
      first article included (``terminal_outcome`` records it so).
    - **not quarantine-counted.** ``cancelled`` is outside ``_TERMINAL_RUN_OUTCOMES``
      by design, so ``recent_terminal_farm_items`` never sees it.
    - **it does not pause the run.** Neither ``_maybe_pause_run_no_printers`` nor
      ``_maybe_pause_run_exhausted`` is evaluated: nothing was exhausted and no printer
      became unavailable.
    - **the plate is the authority's.** A refused plate was gated by the terminal's one
      plate call (``note_terminal`` with the refusal); a fault stop leaves the plate to
      the terminal's own deposit evidence, exactly as ``_on_item_failed`` does. No
      second plate write lives here.

    The requeue itself is ``requeue.requeue_attempt``'s: the plate lands NEXT in line
    (the head of its scope), a pool unit returns to the pool (so the scheduler
    re-searches and may pick another machine), and a genuinely PINNED unit keeps its
    pin — the gate on the held printer is what stops it dispatching there. The source
    row's terminal state is already COMMITTED (``main.on_print_complete`` writes it
    before this task is spawned, and :func:`on_terminal` commits its hygiene first) —
    the verb's own precondition.
    """
    verdict = outcome.verdict if outcome is not None else None
    if batch.status in ("cancelled", "completed"):
        # Same rule as _on_item_failed: a terminal run must never mint a dispatchable
        # unit (R1/R2 — a cancelled run would silently print one more plate).
        logger.info(
            "farm_policy: unit %s ended '%s' but run %s is %s — no requeue",
            item.id,
            verdict,
            batch.id,
            batch.status,
        )
        return

    refused = verdict == farm_correlation.STOP_VERDICT_PLATE_REFUSED
    retry = await requeue.requeue_attempt(
        item.id, cause="plate_check" if refused else "fault_stop", stage_manual=batch.status == "paused"
    )
    logger.info(
        "farm_policy: unit %s requeued as %s — %s; lineage only, no quarantine count, run %s stays %s",
        item.id,
        retry.item_id if retry is not None else "nothing (see the requeue line)",
        "the printer's plate check refused the plate"
        if refused
        else f"operator stopped a print its printer was already holding ({verdict}, faults "
        f"{sorted(outcome.faults_open) if outcome is not None else []})",
        batch.id,
        batch.status,
    )


async def _maybe_lift_held_bed(db: AsyncSession, printer_id: int) -> None:
    """Raise the bed off its bottom stop after the printer refused its plate.

    The firmware parks the bed at the BOTTOM of travel when its HMS pauses the job —
    on this farm that is onto the operator's plate-release aid, where it sits for the
    whole plate-check pause and where the plate is awkward to reach once the job has
    been stopped. So the bed is given a small clearance, once, at the refused plate's
    terminal.

    It is the vendor's own sequence, and every line of it is load-bearing:

    * ``G380 S2 Z<+32>`` — a relative Z move GUARDED by the bottom end stop (the stock
      machine-start block's ``;===== avoid end stop =====``). It drives the bed DOWN,
      away from the nozzle, and truncates to ~0 against the stop the bed is already on.
      Its only job is to make the next move start from a KNOWN physical position.
    * ``G380 S2 Z-<hold_lift_mm>`` — the lift itself, measured from that physical stop
      rather than from the firmware's Z frame, which after a pause at the plate check
      (it runs BEFORE ``G28 Z``) the farm does not trust. The distance that clears the
      operator's aid is a hardware fact the code cannot know (red line 3), so it comes
      from the model's registry row.
    * ``M400`` before ``M18`` — drain the motion queue before releasing the steppers,
      or the bed drops the rest of the way under its own weight (the deep-park's lesson).

    Never an absolute Z and never a bare upward relative move from an unknown position:
    the bed's height at this gate is genuinely unknown (an eject re-parks at
    ``PARK_Z_MM``, a start block pushes ~50 mm down, a pause parks at the bottom), and
    a move that assumes otherwise is the 002-H2S bed-into-the-floor shape.

    Skipped on a bedslinger (the gantry carries Z — there is no bed travel to lift) and
    on a model with no geometry row or no ``z_travel_mm`` (no proven Z axis to move).
    Cosmetic-lane discipline like the idle deep-park: every failure is a log line and
    nothing here raises into the terminal chain.
    """
    try:
        printer = await db.get(Printer, printer_id)
        if printer is None:
            return
        if is_bedslinger_model(printer.model):
            return
        geometry = await eject_geometry.get_geometry(db, printer.model)
        if geometry is None or geometry.z_travel_mm is None:
            logger.info(
                "farm_policy: held-bed lift skipped on printer %s — no z_travel_mm for model %r",
                printer_id,
                printer.model,
            )
            return
        client = printer_manager.get_client(printer_id)
        if client is None:
            logger.warning("farm_policy: held-bed lift skipped on printer %s — no MQTT client", printer_id)
            return
        lift = geometry.hold_lift_mm
        ok = client.send_gcode(
            f"M17\nG91\nG380 S2 Z{VISION_HOLD_PROBE_MM:.1f} F1200\nG380 S2 Z-{lift:.1f} F1200\nG90\nM400\nM18"
        )
        if ok:
            logger.info("farm_policy: held-bed lift sent on printer %s (%.1f mm off the bottom stop)", printer_id, lift)
        else:
            logger.warning("farm_policy: held-bed lift command refused on printer %s", printer_id)
    except Exception:  # noqa: BLE001 — cosmetic lane: never raises into the terminal chain
        logger.warning("farm_policy: held-bed lift failed on printer %s", printer_id, exc_info=True)


# --------------------------------------------------------------------------- #
# Quarantine
# --------------------------------------------------------------------------- #
async def recent_terminal_farm_items(db: AsyncSession, printer_id: int, limit: int) -> list[PrintQueueItem]:
    """The last ``limit`` terminal (completed/failed) farm items on ``printer_id``.

    "Farm" = the item belongs to a batch with a ``sku_file_id``. Ordered
    most-recent-first by ``completed_at``.

    The window STARTS at the printer's ``quarantine_cleared_at`` when one is set:
    the failure streak is derived from queue history, not from a counter, so
    without that cutoff the very history that tripped a quarantine survives the
    operator clearing it and the next single failure re-trips "N consecutive"
    instantly (2026-08-14: "Recover & resume" re-quarantined within seconds, over
    and over). Clearing a quarantine means "an operator inspected this printer;
    count fresh", so pre-recovery outcomes are outside the window by definition.
    NULL — never recovered — counts the whole history, which is the pre-migration
    behaviour. This is the ONE place the streak window is defined; every consumer
    reads it from here.
    """
    printer = await db.get(Printer, printer_id)
    cleared_at = printer.quarantine_cleared_at if printer is not None else None
    stmt = (
        select(PrintQueueItem)
        .join(PrintBatch, PrintQueueItem.batch_id == PrintBatch.id)
        .where(PrintQueueItem.printer_id == printer_id)
        .where(PrintBatch.sku_file_id.is_not(None))
        .where(PrintQueueItem.status.in_(_TERMINAL_RUN_OUTCOMES))
    )
    if cleared_at is not None:
        stmt = stmt.where(PrintQueueItem.completed_at > cleared_at)
    result = await db.execute(stmt.order_by(PrintQueueItem.completed_at.desc()).limit(limit))
    return list(result.scalars().all())


async def quarantine_printer(db: AsyncSession, printer_id: int, reason: str, *, failure_count: int) -> bool:
    """Idempotently quarantine ``printer_id`` with ``reason``.

    Sets the DB flags + reason, commits, mirrors the in-memory
    ``printer_manager`` flag, WARNING-logs, and fires the quarantine notification
    (``failure_count`` = how many failures drove it — the consecutive-failure
    count, or 1 for an immediate cause like an unverified eject). Returns False —
    doing nothing — when the printer is missing or already quarantined. The one
    canonical quarantine mutator shared by the consecutive-failure policy below
    and the eject-verification / cooldown-stall paths.
    """
    printer = await db.get(Printer, printer_id)
    if printer is None or printer.quarantined:
        return False
    printer.quarantined = True
    printer.quarantine_reason = reason
    await db.commit()
    printer_manager.set_quarantined(printer_id, True)
    logger.warning("farm_policy: quarantined printer %s — %s", printer_id, reason)
    await notification_service.on_printer_quarantined(printer_id, printer.name, failure_count, reason, db)
    return True


async def maybe_quarantine_printer(db: AsyncSession, batch: PrintBatch, item: PrintQueueItem) -> bool:
    """Quarantine ``item.printer_id`` when the last N farm outcomes all failed.

    N = the run's ``escalate_consecutive_failures``. Returns True if it tripped.
    Delegates the actual mutation to :func:`quarantine_printer` (the single path).
    """
    printer_id = item.printer_id
    if printer_id is None:
        return False
    threshold = batch.escalate_consecutive_failures if batch.escalate_consecutive_failures else 2
    recent = await recent_terminal_farm_items(db, printer_id, threshold)
    if len(recent) < threshold or not all(r.status == "failed" for r in recent):
        return False

    reason = f"{threshold} consecutive farm print failures"
    return await quarantine_printer(db, printer_id, reason, failure_count=threshold)


async def clear_quarantine(db: AsyncSession, printer_id: int) -> Printer:
    """Clear a printer's quarantine flag + reason (route helper). 404 if missing.

    Also stamps ``quarantine_cleared_at``, which moves the start of the
    consecutive-failure window (see :func:`recent_terminal_farm_items`) — the
    clear and the streak reset are ONE act, so they are one write. This is the
    single quarantine-clearing mutator, shared by the route and by
    :func:`recover_printer`, so both get the reset. Stamped even when the printer
    was not actually quarantined: the operator still asserted they inspected it,
    and an idempotent repeat only moves the window forward over history that is
    already outside it.
    """
    printer = await db.get(Printer, printer_id)
    if printer is None:
        raise HTTPException(status_code=404, detail="Printer not found")
    printer.quarantined = False
    printer.quarantine_reason = None
    printer.quarantine_cleared_at = datetime.now(timezone.utc)
    await db.commit()
    printer_manager.set_quarantined(printer_id, False)
    logger.info("farm_policy: quarantine cleared for printer %s", printer_id)
    return printer


async def recover_printer(db: AsyncSession, printer_id: int) -> dict:
    """One-click operator recovery for a wedged farm printer.

    Collapses the genuine-failure cascade's three manual actions (clear plate →
    clear quarantine → resume run) into one explicit operator override, composing
    the canonical service mutators — no new recovery logic, no dual path. Every
    step is idempotent, so a repeat call is a no-op returning the same shape.

    1. Force-clear everything the occupancy authority believes about the printer —
       the plate gate, any registered eject, any dispatch lease — through
       ``operator_recover``, deliberately WITHOUT the routine clear-plate route's
       live-connection / FINISH-FAILED guard and without its eject-in-flight refusal:
       recover MEANS "an operator inspected the machine", which outranks every stored
       belief. It is gated by its own UI confirm and distinct from the everyday
       empty-bed ack, and what it discarded is logged at WARNING for triage.
    2. Clear any farm quarantine on the printer.
    3. Resume every ``paused`` run that has a queue item on this printer (only
       paused runs — ``transition_run`` 409s otherwise, so filter first).

    Returns ``{"plate_cleared": bool, "quarantine_cleared": bool,
    "runs_resumed": [ids], "incidents_closed": [kinds]}`` — the booleans report whether
    that state was actually changed (was set/quarantined before), and the last names
    the equipment-fault rows step 1 ended. 404 if the printer is unknown. Each per-run
    resume is wrapped so one failure can't abort the whole recovery.
    """
    # Function-level import avoids a circular import (production_run imports
    # farm_policy helpers), matching the fork's style.
    from backend.app.services.production_run import transition_run

    printer = await db.get(Printer, printer_id)
    if printer is None:
        raise HTTPException(status_code=404, detail="Printer not found")

    # 1. Plate-clear gate — explicit override, no connection/state guard.
    plate_cleared = plate_occupancy.is_plate_occupied(printer_id)
    plate_occupancy.operator_recover(printer_id)
    # A hold whose resolution IS the operator taking the part off the plate ends here
    # too — recover is the stronger form of the same statement the clear-plate route
    # makes, and a lost-Z hold left standing after it would keep the chip lit and the
    # eject lane refusing on a printer a human just cleared. WHICH rows it answers is the
    # rule table's, asked inside the lane: a hold the wire owns (a runout, say) is NOT
    # answered by somebody clearing a plate, and neither is a paused plate check (its
    # answer is resuming or stopping the job).
    # ...and it reports WHAT it closed. A Recover whose only effect was ending an
    # equipment fault used to be indistinguishable from one that did nothing at all
    # (011-H2S 2026-09-17).
    incidents_closed = [kind for _id, kind in await pause_recovery.on_plate_cleared(printer_id, recover=True)]

    # 2. Quarantine — idempotent; report whether it was actually set.
    quarantine_cleared = bool(printer.quarantined)
    if quarantine_cleared:
        await clear_quarantine(db, printer_id)

    # 3. Resume runs paused because THIS printer became unavailable (quarantine /
    #    offline). A run paused by a first-article REJECT is deliberately excluded:
    #    reject leaves the rejected part on the plate and resuming re-dispatches a
    #    brand-new first article (transition_run), which would silently undo the
    #    operator's rejection — that run has its own resume affordance on the run
    #    page. Exclude in Python (dialect-safe: first_article_state is NULL for
    #    non-FA runs, which a SQL ``!= 'rejected'`` would wrongly drop).
    #
    #    Restricted to FARM runs (``sku_file_id IS NOT NULL``) — the same predicate
    #    ``spool_recovery._resolve_farm_item`` and ``spool_respool`` use to mean "this
    #    batch is a farm production run". A ``PrintBatch`` left ``paused`` with a
    #    NULL ``sku_file_id`` (SET-NULL after SKU-file deletion, or a plain upstream
    #    batch) reads as 404 to ``transition_run`` — every Recover click then logs a
    #    full traceback per zombie and reports it missing from ``runs_resumed``
    #    (006-H2S 2026-07-26: batches 36/41). Filtering in SQL is the fix; the
    #    per-run catch stays as the belt for genuine transition failures.
    result = await db.execute(
        select(PrintBatch.id, PrintBatch.first_article_state)
        .join(PrintQueueItem, PrintQueueItem.batch_id == PrintBatch.id)
        .where(PrintQueueItem.printer_id == printer_id)
        .where(PrintBatch.status == "paused")
        .where(PrintBatch.sku_file_id.is_not(None))
        .distinct()
    )
    paused_batch_ids = [bid for bid, fa_state in result.all() if fa_state != "rejected"]
    runs_resumed: list[int] = []
    for batch_id in paused_batch_ids:
        try:
            await transition_run(db, batch_id, "resume")
            runs_resumed.append(batch_id)
        except Exception:  # noqa: BLE001 — one run's failure must not abort recovery
            logger.exception("farm_policy: failed to resume run %s while recovering printer %s", batch_id, printer_id)

    logger.info(
        "farm_policy: recovered printer %s (plate_cleared=%s, quarantine_cleared=%s, runs_resumed=%s, "
        "incidents_closed=%s)",
        printer_id,
        plate_cleared,
        quarantine_cleared,
        runs_resumed,
        incidents_closed,
    )
    return {
        "plate_cleared": plate_cleared,
        "quarantine_cleared": quarantine_cleared,
        "runs_resumed": runs_resumed,
        "incidents_closed": incidents_closed,
    }


# --------------------------------------------------------------------------- #
# Run pause / completion
# --------------------------------------------------------------------------- #
def _printer_unavailable(printer: Printer | None) -> bool:
    """A printer is unavailable for a run when quarantined, or connected-then-lost.

    A printer that was never connected in this process (no live status) is
    treated as *unknown*, not offline, so a run isn't spuriously paused at
    startup or in tests where no MQTT session exists.
    """
    if printer is None:
        return True
    if printer.quarantined:
        return True
    status = printer_manager.get_status(printer.id)
    return status is not None and not printer_manager.is_connected(printer.id)


async def _maybe_pause_run_no_printers(db: AsyncSession, batch: PrintBatch) -> None:
    if batch.status != "active":
        return
    result = await db.execute(
        select(PrintQueueItem).where(
            PrintQueueItem.batch_id == batch.id,
            PrintQueueItem.status.in_(("pending", "printing")),
        )
    )
    active_items = list(result.scalars().all())
    printer_ids = {i.printer_id for i in active_items if i.printer_id is not None}
    if not printer_ids:
        return  # model-based / unassigned — scheduler's waiting_reason owns this

    printers = {p.id: p for p in (await db.execute(select(Printer).where(Printer.id.in_(printer_ids)))).scalars().all()}
    if not all(_printer_unavailable(printers.get(pid)) for pid in printer_ids):
        return

    batch.status = "paused"
    # Machine-readable hold reason (Phase 4.1): the run card must distinguish this
    # auto-pause from a manual one. Cleared on resume (transition_run).
    batch.pause_reason = "no_available_printers"
    await db.commit()
    broadcast_production_run_changed(batch.id)
    run = await _load_run(db, batch.id)
    await notification_service.on_run_paused(
        run.name, _sku_code(run), "All selected printers are quarantined or offline", db
    )
    logger.warning("farm_policy: paused run %s — no available printers", batch.id)


async def _maybe_pause_run_exhausted(db: AsyncSession, batch: PrintBatch) -> None:
    """Pause an active run whose last unit exhausted its retries with no work left (R3).

    Without this, a run whose final plate fails past ``retry_max`` sits ``active``
    forever with nothing pending/printing and no notification. Deliberately NOT
    ``_maybe_complete_run`` — completing would hide the shortfall; pausing surfaces a
    Resume affordance whose ``top_up_run`` mints the replacement plates.

    Guards: only an active run; a run still awaiting first-article approval is
    NORMAL with zero live items (operator-gated), so it's exempt; and if ANY item is
    still pending/printing (a just-created retry, or a duplicate failure event racing
    a live retry) there's work in flight — no pause.
    """
    if batch.status != "active":
        return
    if batch.first_article_state == "awaiting_approval":
        return
    result = await db.execute(
        select(PrintQueueItem.id)
        .where(
            PrintQueueItem.batch_id == batch.id,
            PrintQueueItem.status.in_(("pending", "printing")),
        )
        .limit(1)
    )
    if result.first() is not None:
        return

    batch.status = "paused"
    # Machine-readable hold reason (Phase 1): distinguishes retry-exhaustion from the
    # other auto-pauses on the run card. Cleared on resume (transition_run).
    batch.pause_reason = "retries_exhausted"
    await db.commit()
    broadcast_production_run_changed(batch.id)
    run = await _load_run(db, batch.id)
    await notification_service.on_run_paused(
        run.name,
        _sku_code(run),
        "A unit failed with no retries left and the run has no work in flight — Resume creates replacement plates",
        db,
    )
    logger.warning("farm_policy: paused run %s — retries exhausted, no work in flight", batch.id)


async def _maybe_complete_run(db: AsyncSession, batch: PrintBatch) -> None:
    if batch.status != "active":
        return
    # A gated run whose FA isn't approved still has uncreated plates.
    if batch.first_article_state in ("pending_print", "awaiting_approval", "rejected"):
        return
    if batch.first_article_plan:
        return  # deferred plates not yet materialised

    result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.batch_id == batch.id))
    items = list(result.scalars().all())
    if any(i.status in ("pending", "printing") for i in items):
        return
    completed = sum(1 for i in items if i.status == "completed")
    if completed == 0:
        return

    # Planned plate count — the SAME figure the run-detail API reports as
    # ``plates_total`` (single source of truth; lazy import breaks the
    # production_run <-> farm_policy import cycle, as elsewhere in this module).
    from backend.app.services.production_run import planned_plate_count

    planned = planned_plate_count(batch.quantity, items)

    # F2: an operator-stopped unit holds the run SHORT of its plan. Completing the
    # run here (as the plain last-plate path would) strands the documented
    # "Resume tops the deficit back up" affordance — resume of a *completed* run
    # 409s via ``can_transition``. Pause instead, KEEPING ``operator_stop`` so the
    # run card still explains the hold; RESUME's ``top_up_run`` mints the
    # replacement plate(s). Mirrors ``_maybe_pause_run_exhausted``. The
    # retries-exhausted path owns its own (no ``operator_stop``) case.
    if batch.pause_reason == "operator_stop" and completed < planned:
        batch.status = "paused"
        await db.commit()
        broadcast_production_run_changed(batch.id)
        run = await _load_run(db, batch.id)
        deficit = planned - completed
        plate_word = "plate" if deficit == 1 else "plates"
        await notification_service.on_run_paused(
            run.name,
            _sku_code(run),
            f"a stopped unit left this run {deficit} {plate_word} short — Resume creates the replacement {plate_word}",
            db,
        )
        logger.warning(
            "farm_policy: paused run %s — operator stop left it %d plate(s) short (%d/%d completed)",
            batch.id,
            deficit,
            completed,
            planned,
        )
        return

    batch.status = "completed"
    # A completed run must not carry a stale hold reason (e.g. an ``operator_stop``
    # hold whose deficit was later topped up to the full plan). The prod incident
    # left ``pause_reason='operator_stop'`` stamped on a completed run — clear it.
    batch.pause_reason = None
    await db.commit()
    broadcast_production_run_changed(batch.id)
    run = await _load_run(db, batch.id)
    upp = plate_units(run.sku_file.units_per_plate if run.sku_file else None)
    await notification_service.on_run_completed(run.name, _sku_code(run), completed * upp, completed, db)
    logger.info("farm_policy: run %s completed (%d plates)", batch.id, completed)


# --------------------------------------------------------------------------- #
# First-article approve / reject / finalize
# --------------------------------------------------------------------------- #
async def _notify_first_article_pending(
    db: AsyncSession, batch: PrintBatch, item: PrintQueueItem, archive_data: dict | None
) -> None:
    run = await _load_run(db, batch.id)
    printer_name = "Unknown"
    if item.printer_id is not None:
        printer = await db.get(Printer, item.printer_id)
        if printer is not None:
            printer_name = printer.name
    finish_photo_url = (archive_data or {}).get("finish_photo_url")
    image_data = (archive_data or {}).get("image_data")
    await notification_service.on_first_article_pending(
        item.printer_id,
        printer_name,
        run.name,
        _sku_code(run),
        db,
        finish_photo_url=finish_photo_url,
        image_data=image_data,
    )


async def approve_first_article(db: AsyncSession, run_id: int, eject_remotely: bool) -> PrintBatch:
    """Approve a run's first article.

    ``eject_remotely=False``: the operator physically removed the part — clear
    the plate gate (same mechanism as the manual plate-clear confirm), mark
    ``approved``, materialise the remaining plates.

    ``eject_remotely=True``: dispatch a part-present eject-only job; the run stays
    ``awaiting_approval`` until that eject completes, at which point
    ``_finalize_remote_eject`` clears the gate, marks ``approved`` and creates the
    remaining plates. A dispatch failure raises 409/502 and leaves the state as
    ``awaiting_approval``.
    """
    run = await _load_run(db, run_id)
    if run.first_article_state != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"First article is not awaiting approval (state={run.first_article_state})",
        )
    if run.status == "cancelled":
        # An aborted run must never materialise dispatchable plates from an approval
        # (R6: the abort cancelled its pending items; approval would resurrect it).
        raise HTTPException(status_code=409, detail="Cannot approve a first article on a cancelled run")
    # Function-level import avoids a circular import (production_run imports
    # farm_policy helpers), matching the fork's style; _find_fa_item now lives in
    # production_run as the single shared home.
    from backend.app.services.production_run import _find_fa_item

    fa_item = _find_fa_item(run)

    if eject_remotely:
        await _dispatch_remote_eject(db, run, fa_item)
        return await _load_run(db, run_id)

    run.first_article_state = "approved"
    await db.commit()
    broadcast_production_run_changed(run_id)
    approve_printer_id = fa_item.printer_id if fa_item is not None else None
    printer_name: str | None = None
    if approve_printer_id is not None:
        plate_occupancy.clear_plate(approve_printer_id)
        printer = await db.get(Printer, approve_printer_id)
        printer_name = printer.name if printer is not None else None
    run = await _load_run(db, run_id)
    await create_remaining_plates(db, run)
    # Close the loop on the on_first_article_pending alert (Phase 6): the plates
    # are released. Reload so sku_file/sku are fresh, then read the identity args
    # before on_first_article_approved's internal commit expires the row.
    run = await _load_run(db, run_id)
    await notification_service.on_first_article_approved(
        run.name, _sku_code(run), printer_name, db, printer_id=approve_printer_id
    )
    return await _load_run(db, run_id)


async def reject_first_article(db: AsyncSession, run_id: int, reason: str) -> PrintBatch:
    """Reject a run's first article: mark ``rejected``, pause the run, notify.

    The plate gate is left SET — the rejected part is still on the plate for the
    operator to inspect/remove. Resuming a rejected run re-dispatches a new first
    article (see ``services.production_run.transition_run``).
    """
    run = await _load_run(db, run_id)
    if run.first_article_state != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"First article is not awaiting approval (state={run.first_article_state})",
        )
    run.first_article_state = "rejected"
    run.first_article_reject_reason = reason
    run.status = "paused"
    # Machine-readable hold reason (Phase 4.1); cleared on resume (which
    # re-dispatches a fresh first article).
    run.pause_reason = "first_article_rejected"
    await db.commit()
    broadcast_production_run_changed(run_id)
    run = await _load_run(db, run_id)
    await notification_service.on_run_paused(run.name, _sku_code(run), f"First article rejected: {reason}", db)
    return run


async def _finalize_remote_eject(db: AsyncSession, run_id: int, printer_id: int) -> None:
    run = await db.get(PrintBatch, run_id)
    if run is None or run.first_article_state != "awaiting_approval":
        return
    run.first_article_state = "approved"
    await db.commit()
    broadcast_production_run_changed(run_id)
    # The gate itself was already dropped by ``resolve_eject("completed")`` in the
    # terminal branch that called us — this is the belt for the reconciled path, where
    # the same finalisation replays from a downtime FINISH. Idempotent: the authority
    # answers ``not_occupied`` on an already-clear plate.
    plate_occupancy.clear_plate(printer_id)
    printer = await db.get(Printer, printer_id)
    printer_name = printer.name if printer is not None else None
    run = await _load_run(db, run_id)
    await create_remaining_plates(db, run)
    # Same loop-closing notification as the physical-approve path (Phase 6).
    run = await _load_run(db, run_id)
    await notification_service.on_first_article_approved(
        run.name, _sku_code(run), printer_name, db, printer_id=printer_id
    )
    logger.info("farm_policy: remote eject finalised run %s on printer %s", run_id, printer_id)


async def _dispatch_remote_eject(db: AsyncSession, run: PrintBatch, fa_item: PrintQueueItem | None) -> None:
    """FA path: eject the first-article plate, honouring the cooldown release.

    The eject file is MOTION-ONLY — the thermal wait that used to live in its
    G-code is now server policy — so an approval that lands while the bed is
    still hot must NOT sweep immediately. The live bed judged by THE release
    predicate (``shop_air.release_ok`` over the bed, its own chamber air and the
    measured eject line — the same one the cooldown watch uses) says it may go
    (the common case: the bed cooled during inspection) → dispatch NOW through the
    shared ``eject_remote.dispatch_part_present_eject`` (dispatch errors surface to
    the operator as 409/502, exactly as before). Bed still hot or unreadable → arm
    the FA cooldown watch, which dispatches the same eject on the same predicate
    (plateau/cap policy applies); the run stays ``awaiting_approval`` and the UI
    shows the cooldown phase. A unit with no usable eject profile is dispatched
    directly, and the dispatcher's own refusal reaches the operator.

    The plate-clear gate is NOT dropped here: it clears when the eject job's
    terminal arrives (``_finalize_remote_eject`` via ``on_terminal`` step 1).
    An unfinished eject is simply re-approvable, never a half state.
    """
    from backend.app.services.eject import shop_air
    from backend.app.services.eject.monitor import _unit_releasable

    if fa_item is None or fa_item.printer_id is None:
        raise HTTPException(status_code=409, detail="First-article printer is unknown; cannot eject remotely")
    if not printer_manager.is_connected(fa_item.printer_id):
        # Immediate, actionable feedback — a deferred watch on a dead printer would
        # just exit "stale" with the operator none the wiser.
        raise HTTPException(status_code=409, detail="Printer is not connected; cannot eject remotely")

    releasable = await _unit_releasable(fa_item.id, for_first_article=True)
    line = await shop_air.current_line(db)
    state = printer_manager.get_status(fa_item.printer_id)
    readable = state is not None and getattr(state, "connected", False)
    bed = state.temperatures.get("bed") if readable else None
    chamber = (
        shop_air.own_air_c(state.temperatures, model=printer_manager.get_model(fa_item.printer_id))
        if readable
        else None
    )

    if releasable and not shop_air.release_ok(bed, chamber, line.line_c, line.margin_c):
        # Arm the deferred sweep by SWAPPING THE PLATE'S POLICY, not by spawning a
        # watch: the plate is what the FA part sits on, so the FA eject is a property
        # of that plate and the policy driver arms the watch off it. A re-approve while
        # the deferred eject is still cooling is then a no-op by construction — the
        # policy it would set is the one already standing.
        refusal = plate_occupancy.set_policy(fa_item.printer_id, FirstArticleEject(unit_id=fa_item.id, run_id=run.id))
        if refusal is None:
            logger.info(
                "farm_policy: FA eject for run %s deferred — bed %s not released (line %s, chamber %s); "
                "cooldown watch armed",
                run.id,
                f"{bed:.1f}°C" if bed is not None else "unreadable",
                f"{line.line_c:.1f}°C" if line.line_c is not None else "unknown",
                f"{chamber:.1f}°C" if chamber is not None else "none",
            )
            return
        # ``not_occupied``: the plate this approval would sweep is not gated (an
        # operator cleared it, or the gate never rose). There is nothing to eject.
        raise HTTPException(
            status_code=409,
            detail="This printer's plate is not gated, so there is nothing to eject; approve without a remote eject",
        )

    try:
        await eject_remote.dispatch_part_present_eject(
            db,
            printer_id=fa_item.printer_id,
            queue_item_id=fa_item.id,
            purpose="fa",
            run_id=run.id,
        )
    except eject_remote.EjectDispatchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    logger.info("farm_policy: dispatched remote FA eject for run %s on printer %s", run.id, fa_item.printer_id)
