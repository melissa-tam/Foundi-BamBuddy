"""The JOB phase of a print terminal: everything a terminal owes the RECORD of the job that ended.

A print terminal is two different things, and until 2026-09-25 one handler did both as one:

* the **job phase** — the job's archive closed, its print-log row, its unit's end and the unit's
  disposition, the job's uploaded file removed. These are facts about THE JOB THAT ENDED;
* the **printer phase** — the plate, the eject, the incident closers, the swap and de-bounce
  resets, the RFID sweep, the 3MF cache, the print user, the photo/timelapse/notifications/
  energy. These are facts about THE PRINTER, keyed to the job it is running now.

A real MQTT terminal is both at once: the job that ended IS the printer's job. The downtime
reconcile is not — it learns that an archive's job ended at some unobserved time, often while the
printer demonstrably runs ANOTHER job — and replaying it through the whole handler acted on the
running job (RC3, production 2026-09-16 → 09-24: a plate gate raised mid-print behind a 90-minute
"Plate Not Empty" page, a foreign-job page, ~7.2 kg of phantom filament charged from the LIVE
job's progress, the running job's usage session popped, an RFID sweep, swap and de-bounce resets,
and the wire-hold closer ending another job's hold). So the job phase lives here, ONE
implementation of each step, called by BOTH:

* ``main.on_print_complete`` — the real terminal, in its own order, followed by its printer phase;
* the downtime reconcile (``print_reconcile``) and the supersede at print start
  (``print_binding.supersede_other_live``) — for an archive whose terminal the farm never saw,
  which run exactly the job phase their verdict names and never the printer phase.

The steps (the letters are the plan's):

* (a) close the record — ``print_binding.close_archive`` (THE printing → terminal writer) and
  :func:`announce_archive_closed`, the websocket and relay events that follow a close;
* (b) the run's print-log row — :func:`write_run_log`;
* (c) the unit's end — :func:`record_unit_outcome` (``queue_transitions`` writes the row; the
  library-usage bump stays here, in the job phase);
* (d) the unit's disposition — ``farm_policy.on_unit_terminal`` (:func:`dispose_unit` for a lane
  that has no session of its own);
* (e) the job's uploaded file — :func:`delete_uploaded_file` (#374 / #1542);
* (f) the job's filament charge — :func:`charge_usage`, THE one charge step (the internal inventory
  and the Spoolman lane alike), on the outcome's own ``charge`` basis (``terminal_outcome.ChargeBasis``)
  from the terminal payload's evidence. The print-log row's grams (b) follow the same basis.

A job phase run for an unobserved terminal charges nothing — nobody measured it: its outcome is
built by :func:`unobserved_outcome` under the ``reconcile_unknown`` verdict, whose basis is
``none`` by construction, so ``close_superseded`` and ``close_observed`` have no charge to make and
make none.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import exists, select

# Through the module, never ``from … import async_session``: the session a lane opens is read at
# CALL time, so the one test patch of ``core.database.async_session`` reaches every lane here.
from backend.app.core import database as _database
from backend.app.core.websocket import ws_manager
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_log import PrintLogEntry
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services import farm_policy, print_binding
from backend.app.services.farm_correlation import STOP_SOURCE_RECONCILE_UNKNOWN
from backend.app.services.mqtt_relay import mqtt_relay
from backend.app.services.plate_occupancy import DepositEvidence
from backend.app.services.print_log import write_log_entry
from backend.app.services.printer_manager import printer_manager
from backend.app.services.queue_transitions import (
    STOP_SOURCE_QUEUE_PAGE,
    UNIT_TERMINAL_STATUSES,
    annotate_stopped_unit,
    record_unit_terminal,
)
from backend.app.services.terminal_outcome import ChargeBasis, TerminalOutcome, build_terminal_outcome
from backend.app.utils.filename import derive_remote_filename

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# The printer's raw word for a job whose end nobody observed: neither FINISH nor FAILED was seen.
# ``terminal_outcome`` records it ``cancelled`` under the ``reconcile_unknown`` verdict.
_UNOBSERVED_RAW_STATUS = "aborted"


# --------------------------------------------------------------------------- #
# (a) close the record
# --------------------------------------------------------------------------- #
async def announce_archive_closed(archive_id: int, *, status: str, print_name: str | None) -> None:
    """The events a closed archive owes the UI and the MQTT relay — sent AFTER the close commits.

    Only the closer that WON ``print_binding.close_archive`` announces: a closer that lost the
    race wrote nothing, and announcing its status would contradict the row.
    """
    try:
        await ws_manager.send_archive_updated({"id": archive_id, "status": status})
        logger.info("[ARCHIVE] WebSocket notification sent for archive %s", archive_id)
    except Exception as e:  # noqa: BLE001 — an event must never fail the terminal
        logger.warning("[ARCHIVE] WebSocket archive_updated failed for archive %s: %s", archive_id, e)
    try:
        await mqtt_relay.on_archive_updated(archive_id=archive_id, print_name=print_name, status=status)
    except Exception:  # noqa: BLE001 — the relay is best-effort (upstream behaviour)
        pass


# --------------------------------------------------------------------------- #
# (b) the run's print-log row
# --------------------------------------------------------------------------- #
def compute_run_filament_grams(
    charge: ChargeBasis,
    archive_filament_used_grams: float | None,
    last_progress: float | int | None,
    usage_results: list[dict] | None,
) -> float | None:
    """Per-run filament for PrintLogEntry, partial- and tracker-aware (#1378, #1390).

    The grams a run is RECORDED as having used follow the same basis as the grams it is
    CHARGED (``terminal_outcome.ChargeBasis``), so the print log (Stats, the accounting feed)
    and the spool ledger tell one story. Priority:
        1. Sum of tracked spool deltas in ``usage_results`` (AMS-measured
           weight delta — same source that drives "Total Consumed" on the
           Inventory page, so Stats and Inventory totals stay aligned).
        2. ``full``: the slicer estimate (no tracker available, fall back to
           the canonical "this print used X" value).
        3. ``partial``: ``estimate * last_progress%`` — the job's OWN last
           progress, off its terminal payload, never the live printer's.
        4. ``None`` — a ``none`` basis, or a partial with nothing to scale: nobody
           measured the run, so the row states no grams rather than inventing them.
    """
    tracked_grams = sum(r.get("weight_used") or 0 for r in (usage_results or []))
    if tracked_grams > 0:
        return round(tracked_grams, 1)

    if charge == "full":
        return archive_filament_used_grams

    if charge == "partial" and archive_filament_used_grams:
        scale = max(0.0, min(((last_progress or 0) / 100.0), 1.0))
        if scale > 0:
            return round(archive_filament_used_grams * scale, 1)

    return None


async def write_run_log(
    db: AsyncSession,
    archive: PrintArchive,
    *,
    printer_id: int,
    printer_name: str | None,
    status: str,
    charge: ChargeBasis,
    last_progress: float | int | None,
    usage_results: list[dict] | None,
    print_user: dict | None,
) -> PrintLogEntry:
    """Write the print-log row of the run ``archive`` records — one per closed attempt.

    A separate table that never touches the archive's own figures: per-run actuals (#1378), so
    Stats reflect what THIS print used, not the archive's first-run values. ``archive`` must be
    read AFTER its close (``completed_at`` and ``failure_reason`` are the close's).

    ``status`` is the recorded word; the grams and the cost follow ``charge`` — the run's charge
    basis — and ``last_progress``, the job's own last progress off its terminal payload (None
    where no terminal was observed), exactly as the spool charge does
    (:func:`compute_run_filament_grams`).

    Back-fills ``created_by_id`` on an unattributed archive from the print-session user (#730):
    an archive auto-created from a printer-initiated print stays unattributed otherwise; an
    existing attribution is never overwritten.

    Does not commit.
    """
    print_user_id = print_user.get("user_id") if print_user else None
    if archive.created_by_id is None and print_user_id is not None:
        archive.created_by_id = print_user_id
    run_grams = compute_run_filament_grams(charge, archive.filament_used_grams, last_progress, usage_results)

    # Per-run cost — prefer the usage_results sum. For partial prints the topup-to-estimate logic
    # in usage_tracker (which assumes the print completed) is deliberately skipped; the raw
    # tracked-spool sum is closer to what THIS run actually cost.
    run_cost: float | None = None
    if usage_results:
        run_cost = sum(r.get("cost") or 0 for r in usage_results) or None
    if run_cost is None and charge == "full":
        run_cost = archive.cost

    return await write_log_entry(
        db,
        archive_id=archive.id,
        status=status,
        print_name=archive.print_name,
        printer_name=printer_name,
        printer_id=printer_id,
        started_at=archive.started_at,
        completed_at=archive.completed_at,
        filament_type=archive.filament_type,
        filament_color=archive.filament_color,
        filament_used_grams=run_grams,
        cost=run_cost,
        failure_reason=archive.failure_reason,
        thumbnail_path=archive.thumbnail_path,
        created_by_id=archive.created_by_id,
        created_by_username=print_user.get("username") if print_user else None,
    )


async def run_has_log(db: AsyncSession, archive: PrintArchive) -> bool:
    """Does the run ``archive`` records already have its print-log row?

    One archive records one attempt (``print_binding``), so the run's row is one written against
    this archive since its print was bound — ``created_at`` at or after ``started_at``. A legacy
    archive that several reprints re-used carries its earlier runs' rows too; those were written
    before this run started and are not this run's.
    """
    stmt = select(PrintLogEntry.id).where(PrintLogEntry.archive_id == archive.id)
    if archive.started_at is not None:
        stmt = stmt.where(PrintLogEntry.created_at >= archive.started_at)
    return bool(await db.scalar(select(exists(stmt))))


# --------------------------------------------------------------------------- #
# (c) the unit's end
# --------------------------------------------------------------------------- #
async def bump_library_file_usage_if_completed(db: AsyncSession, item: PrintQueueItem, queue_status: str) -> None:
    """Increment LibraryFile.print_count and stamp last_printed_at when a queued
    print completes successfully. Gated to status=='completed': failed, cancelled
    and aborted prints do not count as usage. Caller is responsible for committing
    the session. No-op when the queue item has no linked library file (e.g. reprints
    from an archive). See #1008."""
    if queue_status != "completed" or item.library_file_id is None:
        return
    lib_file = await db.scalar(select(LibraryFile).where(LibraryFile.id == item.library_file_id))
    if lib_file is None:
        return
    lib_file.print_count = (lib_file.print_count or 0) + 1
    lib_file.last_printed_at = datetime.now(timezone.utc)


@dataclass(frozen=True)
class RecordedUnit:
    """A unit whose end THIS terminal recorded (or, for a queue-page stop, annotated)."""

    item_id: int
    status: str
    auto_off_after: bool


async def record_unit_outcome(
    db: AsyncSession,
    item_id: int,
    outcome: TerminalOutcome,
    *,
    completed_at: datetime,
) -> RecordedUnit | None:
    """Record ``outcome`` on the unit a terminal was attributed to; None when there is nothing to record.

    A ``printing`` unit ENDS here, through ``queue_transitions.record_unit_terminal`` (the one
    writer of a unit's end, conditional on ``printing``): the outcome's word — never
    ``aborted``, the builder records an unattributed abort as ``cancelled`` — its verdict as the
    stop attribution when it was a stop, and the printer's own words for a terminal that ended
    without its part, unless the row already carries words of its own.

    A unit the QUEUE PAGE's Stop already ended (``cancelled`` / ``operator_ui``, committed before
    the terminal could arrive) is not ended again: its status and stop time stay the route's,
    and the terminal records only what the terminal knows (``queue_transitions
    .annotate_stopped_unit``) — ONCE. The row's own shape is the evidence (the correlation owner
    matched it by dispatch id); a second terminal for the same stopped job finds it answered and
    is owed nothing, exactly as a second terminal for a unit its first one ended.

    The library-usage bump (#1008) rides the same transaction. Does not commit.
    """
    item = await db.get(PrintQueueItem, item_id)
    if item is None:
        return None
    queue_stopped = item.status == "cancelled" and item.stop_source == STOP_SOURCE_QUEUE_PAGE
    if item.status != "printing" and not queue_stopped:
        return None
    status = outcome.recorded_status
    # The terminal's verdict IS the unit's stop attribution (the closed ``StopVerdict`` set) —
    # lineage and history only: the farm policy reads the verdict off the outcome.
    stop_source = outcome.verdict if status == "cancelled" and outcome.verdict is not None else None
    message = (
        outcome.printer_message
        if status != "completed" and outcome.printer_message and not item.error_message
        else None
    )
    if queue_stopped:
        if not await annotate_stopped_unit(
            db, item.id, answered_at=completed_at, stop_source=stop_source, error_message=message
        ):
            return None
    elif not await record_unit_terminal(
        db, item.id, status=status, completed_at=completed_at, stop_source=stop_source, error_message=message
    ):
        return None
    await db.refresh(item)
    await bump_library_file_usage_if_completed(db, item, status)
    return RecordedUnit(item_id=item.id, status=status, auto_off_after=bool(item.auto_off_after))


def unobserved_outcome(archive: PrintArchive, unit: PrintQueueItem) -> TerminalOutcome:
    """THE outcome of a unit whose print ended where the farm could not see it.

    Built through the one builder, from what is actually known: the printer never said how the
    job ended (``aborted`` — neither FINISH nor FAILED was observed), the verdict is the one no
    actor produced (``reconcile_unknown``), no hold on the printer explains THIS job (its holds
    belong to whatever the printer runs now), and no deposit was measured (fail-closed). The
    builder records it ``cancelled``: nothing was observed to fail, so it feeds neither retry nor
    quarantine, and the disposition is the operator-stop one — the run holds for a human.
    """
    return build_terminal_outcome(
        raw_status=_UNOBSERVED_RAW_STATUS,
        verdict=STOP_SOURCE_RECONCILE_UNKNOWN,
        open_incidents=(),
        job_id=archive.subtask_id,
        evidence=DepositEvidence.unknown(_UNOBSERVED_RAW_STATUS),
        first_article=bool(unit.first_article),
        is_eject=False,
        hms_errors=None,
    )


# --------------------------------------------------------------------------- #
# (d) the unit's disposition
# --------------------------------------------------------------------------- #
async def dispose_unit(item_id: int, outcome: TerminalOutcome) -> None:
    """``farm_policy.on_unit_terminal`` for a lane with no session of its own — its own unit of work.

    Runs AFTER the unit's end is committed: the requeue a disposition may mint refuses a source
    that is not terminal in the database. The UNIT half only — never ``on_terminal``'s printer
    steps (a pending eject's resolution, the refused-plate bed lift): the lanes that call this
    act on a job whose printer runs something else.
    """
    async with _database.async_session() as db:
        await farm_policy.on_unit_terminal(db, item_id, outcome.recorded_status, outcome=outcome)


# --------------------------------------------------------------------------- #
# (e) the job's uploaded file
# --------------------------------------------------------------------------- #
def upload_candidates(archive_filename: str | None, subtask_name: str | None) -> list[str]:
    """The printer-storage paths a job's upload may sit at, most specific first. ONE spelling, for
    the cleanup and for the paths a live job keeps (:func:`delete_uploaded_file`'s ``keep_paths``).

    Primary: the exact path the dispatcher uploaded to — derived from the archive's filename by
    the same rule as the upload (``derive_remote_filename``). Without it, a library row that ended
    up with a doubled ``.gcode.3mf`` (#1542) leaves the real file behind, because the subtask-name
    fallbacks below do not match what is on the card. The fallbacks remain for archive-less prints
    and for older naming variants.
    """
    candidates: list[str] = []
    if archive_filename:
        candidates.append(f"/{derive_remote_filename(archive_filename)}")
    if subtask_name:
        for ext in (".3mf", ".gcode"):
            fallback = f"/{subtask_name}{ext}"
            if fallback not in candidates:
                candidates.append(fallback)
    return candidates


async def _dispatched_filename(db: AsyncSession, unit: PrintQueueItem) -> str | None:
    """The source file name a unit's dispatch was uploaded under — the scheduler's own rule: its
    donor archive's filename, else its library file's (``print_scheduler._start_print``)."""
    if unit.archive_id is not None:
        return await db.scalar(select(PrintArchive.filename).where(PrintArchive.id == unit.archive_id))
    if unit.library_file_id is not None:
        return await db.scalar(select(LibraryFile.filename).where(LibraryFile.id == unit.library_file_id))
    return None


async def live_upload_paths(
    db: AsyncSession, printer_id: int, live_job: str | None, live_subtask_name: str | None
) -> frozenset[str]:
    """The paths the printer's LIVE job may be printing from — the ones a stale job's cleanup keeps.

    A farm runs one file N times, so a stale job and the job the printer runs now usually share
    ONE upload path (``derive_remote_filename`` of the same donor): deleting "the stale job's
    file" would delete the running job's. The job phase acts on the job that ended and never on
    the running one — the whole of RC3. The live job's paths are its own name's fallbacks and,
    for a farm dispatch, its unit's upload path.
    """
    paths = set(upload_candidates(None, live_subtask_name))
    unit = await print_binding.dispatched_unit(db, printer_id, live_job)
    if unit is not None:
        paths.update(upload_candidates(await _dispatched_filename(db, unit), None))
    return frozenset(paths)


async def delete_uploaded_file(
    printer_id: int,
    *,
    subtask_name: str | None,
    archive_id: int | None,
    keep_paths: Collection[str] = (),
) -> None:
    """Delete an ended job's uploaded file from the printer's storage (Issue #374, #1542).

    The scheduler uploads files to the storage root (/). Some printers (e.g. P1S, A1) auto-start
    files found in the root on a power cycle, causing ghost prints — and a job whose terminal the
    farm never saw is exactly the one whose file is still there. ``keep_paths`` are paths a LIVE
    job may be printing from (:func:`live_upload_paths`); they are never deleted.

    Three outcomes track across all candidates so the final log line reflects what actually
    happened. The A1 in #1721 always ends here with ``any_not_found=True`` and the others False —
    its firmware auto-cleans the card before this cleanup runs, every candidate FTP-DELE returns
    550, and the old code burned 3 retries × 2 s × 3 candidates per print logging a misleading
    "may linger" WARNING on a successful print.

    Best-effort: every failure is a log line.
    """
    if not subtask_name:
        return
    try:
        archive_filename: str | None = None
        async with _database.async_session() as db:
            printer = await db.scalar(select(Printer).where(Printer.id == printer_id))
            if archive_id:
                archive_filename = await db.scalar(select(PrintArchive.filename).where(PrintArchive.id == archive_id))
        if not printer:
            return

        from backend.app.services.bambu_ftp import DeleteResult, delete_file_async

        kept = set(keep_paths)
        candidates = upload_candidates(archive_filename, subtask_name)
        candidate_paths = [path for path in candidates if path not in kept]
        if kept_live := [path for path in candidates if path in kept]:
            logger.info(
                "SD card cleanup on %s keeps %s — the printer's live job may be printing from it",
                printer.name,
                kept_live,
            )

        any_deleted = False
        any_real_failure = False
        any_not_found = False

        for remote_path in candidate_paths:
            # Retry only the FAILED case — 550 NOT_FOUND will never recover by waiting, so a "file
            # isn't here" answer advances immediately to the next candidate without consuming the
            # retry budget.
            for attempt in range(1, 4):
                try:
                    delete_result = await delete_file_async(
                        printer.ip_address,
                        printer.access_code,
                        remote_path,
                        printer_model=printer.model,
                    )
                except Exception as e:  # noqa: BLE001 — one candidate's failure is a retry, not a crash
                    delete_result = DeleteResult.FAILED
                    logger.warning("SD card cleanup attempt %d/3 raised for %s: %s", attempt, remote_path, e)

                if delete_result == DeleteResult.DELETED:
                    any_deleted = True
                    logger.info("Deleted %s from printer %s SD card", remote_path, printer.name)
                    break
                if delete_result == DeleteResult.NOT_FOUND:
                    any_not_found = True
                    break  # 550 will not recover; try next candidate
                # FAILED: real error — retry with backoff, then give up
                if attempt < 3:
                    await asyncio.sleep(2)
                else:
                    any_real_failure = True
                    logger.warning(
                        "SD card cleanup failed after 3 attempts for %s "
                        "(network/auth/transient error — file may linger on SD card)",
                        remote_path,
                    )

        if not any_deleted and not any_real_failure and any_not_found:
            # Every candidate said "not here." Either the printer firmware swept the card itself
            # (common on A1) or the dispatcher's upload path doesn't match the candidate rule.
            # Either way: nothing to clean up, no warning.
            logger.debug(
                "SD card cleanup: nothing to delete on %s — every candidate returned 550 (printer likely self-cleaned)",
                printer.name,
            )
    except Exception as e:  # noqa: BLE001 — a cleanup failure must never fail the terminal
        logger.warning("SD card file cleanup failed for printer %s: %s", printer_id, e)


# --------------------------------------------------------------------------- #
# (f) the job's filament charge
# --------------------------------------------------------------------------- #
async def charge_usage(
    printer_id: int,
    data: dict,
    outcome: TerminalOutcome,
    *,
    archive_id: int | None,
    ams_mapping: list[int] | None,
) -> list[dict]:
    """Charge the job that ended — THE one charge step, for both inventories, on one basis.

    What is charged is ``outcome.charge`` (``full`` / ``partial`` / ``none``), decided once by the
    outcome's builder; how far the job ran and which trays fed it is the terminal payload ``data``.

    * the internal inventory (Spoolman off) — THE one call into ``usage_tracker.on_print_complete``.
      A ``none`` basis still reaches the tracker, which consumes the ending job's usage session (a
      session belongs to its job and only that job's terminal may take it) and charges nothing.
      Broadcasts ``spool_usage_logged`` when something was charged; returns the charged rows (the
      print-log row and the notification read them).
    * the Spoolman lane — :func:`_charge_spoolman`, for any terminal with an archive (its tracking
      row is keyed by one), whatever the setting says now: the row was written when Spoolman was on
      at print start, and each lane asks the setting itself before it reports.

    Each lane is guarded on its own — a failure is a WARNING and the terminal goes on.
    """
    from backend.app.api.routes.settings import get_setting
    from backend.app.services import usage_tracker

    results: list[dict] = []
    try:
        async with _database.async_session() as db:
            spoolman_on = await get_setting(db, "spoolman_enabled")
        if not (spoolman_on and spoolman_on.lower() == "true"):
            async with _database.async_session() as db:
                results = await usage_tracker.on_print_complete(
                    printer_id,
                    data,
                    printer_manager,
                    db,
                    charge=outcome.charge,
                    archive_id=archive_id,
                    ams_mapping=ams_mapping,
                )
            if results:
                await ws_manager.broadcast({"type": "spool_usage_logged", "printer_id": printer_id, "usage": results})
    except Exception as e:  # noqa: BLE001 — a charge failure must never fail the terminal
        logger.warning("[JOB-TERMINAL] usage charge failed for printer %s archive %s: %s", printer_id, archive_id, e)
        results = []
    if archive_id:
        await _charge_spoolman(printer_id, data, outcome, archive_id=archive_id)
    return results


async def _charge_spoolman(printer_id: int, data: dict, outcome: TerminalOutcome, *, archive_id: int) -> None:
    """The Spoolman lane of the charge, on the outcome's basis — every basis retires the tracking row.

    ``full`` reports the plate's usage (``spoolman_tracking.report_usage``); ``partial`` reports the
    share the job's OWN evidence measures (its terminal payload — ``usage_tracker.JobEvidence``,
    never the live printer's layer); ``none`` reports nothing — nobody measured the job.
    """
    from backend.app.services import spoolman_tracking
    from backend.app.services.usage_tracker import JobEvidence

    try:
        if outcome.charge == "full":
            await spoolman_tracking.report_usage(printer_id, archive_id)
            return
        evidence = JobEvidence.from_payload(data) if outcome.charge == "partial" else None
        async with _database.async_session() as db:
            await spoolman_tracking.cleanup_tracking(printer_id, archive_id, db, evidence=evidence)
    except Exception as e:  # noqa: BLE001 — the Spoolman lane must never fail the terminal
        logger.warning("[SPOOLMAN] usage reporting failed for printer %s archive %s: %s", printer_id, archive_id, e)


# --------------------------------------------------------------------------- #
# The job phase of an archive whose terminal the farm never saw
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ClosedJob:
    """An archive THIS call closed for an unobserved terminal, and the job phase owed after the commit.

    Built inside the closing transaction (the close, the unit's end and the print-log row are
    atomic with whatever caused them — a supersede lands in the same transaction as the binding
    that caused it); :func:`settle` runs the rest once that transaction has committed: the events,
    the unit's disposition (its requeue needs the unit's end COMMITTED) and the file cleanup.
    """

    printer_id: int
    archive_id: int
    status: str
    print_name: str | None
    unit_id: int | None = None
    outcome: TerminalOutcome | None = None
    delete_upload: bool = False
    keep_paths: frozenset[str] = field(default_factory=frozenset)


def _printer_name(printer_id: int) -> str | None:
    info = printer_manager.get_printer(printer_id)
    return info.name if info else None


async def close_observed(
    db: AsyncSession, archive: PrintArchive, run_unit: PrintQueueItem, *, printer_id: int
) -> ClosedJob | None:
    """Verdict ``observed``: the archive's run already RECORDED how it ended — close the record to match.

    The unit's own terminal happened (a real terminal that could not find this archive — the
    RC1 leak — a queue-page stop of an offline printer, a dispatch-time failure), so the record
    takes the unit's status and ``completed_at``, and the print-log row that terminal could not
    write is written now from the unit's outcome — unless the run already has one. The unit is
    NOT written (its end is recorded), nothing is charged (nobody measured it here), no plate is
    touched. None when another closer won the archive.
    """
    if run_unit.status not in UNIT_TERMINAL_STATUSES:
        raise ValueError(f"close_observed needs a unit that ended; unit {run_unit.id} is {run_unit.status!r}")
    if not await print_binding.close_archive(
        db, archive.id, status=run_unit.status, completed_at=run_unit.completed_at, failure_reason=None
    ):
        return None
    await db.refresh(archive)
    if not await run_has_log(db, archive):
        await write_run_log(
            db,
            archive,
            printer_id=printer_id,
            printer_name=_printer_name(printer_id),
            status=run_unit.status,
            # The unit's own terminal recorded how the run ended: a completed run ran the whole
            # plate; any other end was measured by nobody here (no peaks reach this lane).
            charge="full" if run_unit.status == "completed" else "none",
            last_progress=None,
            usage_results=None,
            print_user=None,
        )
    logger.info(
        "[JOB-TERMINAL] printer %s: archive %s closed %s from its run unit %s (terminal already recorded)",
        printer_id,
        archive.id,
        run_unit.status,
        run_unit.id,
    )
    return ClosedJob(
        printer_id=printer_id, archive_id=archive.id, status=run_unit.status, print_name=archive.print_name
    )


async def close_superseded(
    db: AsyncSession,
    archive: PrintArchive,
    run_unit: PrintQueueItem | None,
    *,
    printer_id: int,
    reason: str | None,
    keep_paths: frozenset[str],
    now: datetime,
) -> ClosedJob | None:
    """Verdict ``superseded``: the archive's job is over and NOBODY observed how — record it unknown.

    The printer runs (or ran) another job, or reports no job at all, so there is no FINISH or
    FAILED to believe. The record closes ``cancelled`` (``reason`` says why when it is known —
    ``print_binding.SUPERSEDED_REASON`` when the printer has moved on to another job); a unit
    still ``printing`` for it ends ``cancelled`` / ``reconcile_unknown`` through the one writer of
    a unit's end, and its disposition (the run holds for a human) is owed after the commit; the
    run's ``cancelled`` print-log row is written; the job's uploaded file is owed a delete, the
    live job's ``keep_paths`` excepted. NOTHING is charged, and no printer step runs — the plate,
    the eject, the holds and the resets are the running job's. None when another closer won.
    """
    if not await print_binding.close_archive(
        db, archive.id, status="cancelled", completed_at=now, failure_reason=reason
    ):
        return None
    await db.refresh(archive)
    unit_id: int | None = None
    outcome: TerminalOutcome | None = None
    if run_unit is not None and run_unit.status == "printing":
        unit_outcome = unobserved_outcome(archive, run_unit)
        recorded = await record_unit_outcome(db, run_unit.id, unit_outcome, completed_at=now)
        if recorded is not None:
            unit_id, outcome = recorded.item_id, unit_outcome
    await write_run_log(
        db,
        archive,
        printer_id=printer_id,
        printer_name=_printer_name(printer_id),
        status="cancelled",
        charge="none",  # an unobserved end — :func:`unobserved_outcome`'s basis, by construction
        last_progress=None,
        usage_results=None,
        print_user=None,
    )
    logger.warning(
        "[JOB-TERMINAL] printer %s: archive %s (job %r) ended unobserved — closed cancelled, outcome unknown%s",
        printer_id,
        archive.id,
        archive.subtask_id,
        f"; unit {unit_id} recorded cancelled/{STOP_SOURCE_RECONCILE_UNKNOWN}" if unit_id is not None else "",
    )
    return ClosedJob(
        printer_id=printer_id,
        archive_id=archive.id,
        status="cancelled",
        print_name=archive.print_name,
        unit_id=unit_id,
        outcome=outcome,
        delete_upload=True,
        keep_paths=keep_paths,
    )


async def settle(job: ClosedJob) -> None:
    """The job phase owed after a :class:`ClosedJob`'s transaction committed. Each step guarded."""
    await announce_archive_closed(job.archive_id, status=job.status, print_name=job.print_name)
    if job.unit_id is not None and job.outcome is not None:
        try:
            await dispose_unit(job.unit_id, job.outcome)
        except Exception:  # noqa: BLE001 — the disposition is logged by its owner; never raised here
            logger.exception("[JOB-TERMINAL] disposition of unit %s failed", job.unit_id)
    if job.delete_upload:
        await delete_uploaded_file(
            job.printer_id, subtask_name=job.print_name, archive_id=job.archive_id, keep_paths=job.keep_paths
        )
