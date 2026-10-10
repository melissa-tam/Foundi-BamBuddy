"""One-time repair of the three jobs that printed AIR on 2026-10-09/10 (observed-incidents shape 46).

The recovery driver's queued filament pull-back emptied the toolhead during a paused jam recovery
(the AMS ran it ~4.5 min after the driver had given up on it), and a resume then printed with
nothing fed. Two of the jobs ran on to FINISH and were recorded ``completed``, charged for the whole
plate. The operator stopped the third, which is correctly ``cancelled``, but it was charged as if it
had fed through its last layer. The forward fix (the terminal's measured fed extent) cannot reach a
terminal already recorded. Per job, this repair records the two FINISHes ``failed`` /
``printed_without_filament`` with the forward terminal's own ``error_message`` sentence. It
re-charges every usage row of all three to the measured fed extent, credits each spool its
over-charge, and restates the print-log row's grams and cost and the archive's cost the same way.

**Literals, not a rule.** Unlike the replay repair beside it, this one does not plan from the live
database: the rows and their figures come from a READ-ONLY offline report (provenance on
``UNFED_TAIL_JOBS``), and every fact the report saw is re-verified per row at boot before anything
is written.

**Standalone by construction.** It imports only the standard library, SQLAlchemy and the replay
repair's ``RepairDrift`` (itself standalone), never a service with live dependencies, and works on
the migration's own ``AsyncConnection``. ``core.database.run_migrations`` owns the marker, the
outer savepoint and the logging; :func:`repair_unfed_tail_20261010` is all it calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from backend.app.services.foreign_replay_repair import RepairDrift


@dataclass(frozen=True, slots=True)
class UnfedCharge:
    """One ``spool_usage_history`` row of an air-printed job: as written, and as the split owes it."""

    usage_id: int
    spool_id: int
    grams_before: float
    cost_before: float
    percent_before: int
    grams_after: float
    cost_after: float
    percent_after: int
    #: The spool's UNROUNDED over-increment: what the live split added to ``spool.weight_used``
    #: minus what the split owes at the fed extent (the usage row itself stores the 0.1 g rounding).
    spool_credit_g: float


@dataclass(frozen=True, slots=True)
class UnfedTailJob:
    """One job that printed with nothing fed past ``fed_extent``, as the offline report read it.

    ``status_before`` is what the unit, the archive, the print-log row and every usage row recorded.
    ``status_after`` is what the job owes. For the two FINISHes that is ``failed``, and the unit's
    ``error_message`` becomes the forward terminal's sentence. For the operator's stop it is the same
    ``cancelled``: its message stays, and only its grams change.
    """

    printer_id: int
    unit_id: int
    archive_id: int
    job_id: str
    fed_extent: int
    status_before: str
    status_after: str
    error_message_before: str | None
    error_message_after: str | None
    failure_reason_before: str | None
    failure_reason_after: str | None
    archive_cost_before: float
    archive_cost_after: float
    log_entry_id: int
    log_grams_before: float
    log_cost_before: float
    log_grams_after: float
    log_cost_after: float
    charges: tuple[UnfedCharge, ...]


#: The archive's / print log's ``failure_reason`` for a FINISH whose measured fed extent ends
#: before the last layer: the forward terminal classifier's category, spelled exactly as it is
#: (``terminal_outcome.PRINTED_WITHOUT_FILAMENT_CATEGORY``; pinned equal by the repair's test).
FAILURE_CATEGORY: Final = "printed_without_filament"

#: Every float this repair compares is a stored figure; equal means equal to this slack.
_EPSILON: Final = 1e-6

# The literals, from the READ-ONLY offline report of 2026-10-10. The report fetched each archive's
# own 3MF from production (sha256 = the archive's ``content_hash``). It ran the forward split
# ITSELF, ``usage_tracker._track_from_3mf``, then at the blob in HEAD, twice over each file:
#
# * once on the live terminal's own inputs: the charge basis, the PRINT COMPLETE line's
#   ``JobEvidence``, the logged ``ams_mapping``, and the logged tray->spool resolution. That run
#   reproduced all six production usage rows exactly (spool, grams, cost);
# * once as a ``partial`` charge at ``last_layer_num = fed_extent``, over the job's full
#   ``tray_change_log``.
#
# ``fed_extent`` is the layer of the farm's LAST replacement load: the final ``tray_change_log``
# entry and its "Tray change during print" log line (011: tray 1 at layer 93, 04:25:10 EDT;
# 014 #536: tray 0 at layer 9, 00:11:30 EDT; 014 #558: tray 0 at layer 134, 02:39:33 EDT). The
# driver gave up on that load ``unload_failed`` and the AMS later ran its queued pull-back with
# the print still paused. Every resume from then on printed with nothing fed. The split charges
# a segment boundary through the end of the layer it was logged on, so layer ``fed_extent``
# itself stays with the tray that was feeding at the jam, and every later layer owes nothing.
# Owed totals: 352.4 g (011), 103.0 g (014 #536), 410.9 g (014 #558); each equals the plate's
# cumulative per-layer grams through ``fed_extent``. Cost is the split's own,
# ``default_filament_cost`` 12.0/kg (every spool's ``cost_per_kg`` is NULL), and the print-log
# figures are ``job_terminal.write_run_log``'s (the tracked sum and its cost sum, to the cent).
# The archive's ``filament_used_grams`` is the plate's slicer estimate, never a per-run figure,
# and stays. Its ``cost`` is the usage tracker's archive-cost formula as the forward terminal now
# runs it (``usage_tracker.on_print_complete``: the tracked cost, plus the untracked remainder of
# the plate's CHARGED share, ``job_extent.plate_share``, at the default rate), over the repaired
# charges and the fed share (``JobExtent.charged_extent``: L of 167 layers, 0.556886 / 0.053892 /
# 0.802395 of the plate). That share of the estimate (243.1 / 23.5 / 350.3 g) is below the tracked
# grams, so nothing is untracked and the cost is the tracked cost: 4.23 / 1.23 / 4.93.
# ``error_message`` is ``terminal_outcome._printed_without_filament_sentence``'s wording for a
# FINISH over unfed layers ("Printed without filament from layer L of N", N = 167, the plate's
# layer count); production holds NULL on both units today. The operator's stop keeps the message
# its terminal wrote.
UNFED_TAIL_JOBS: Final[tuple[UnfedTailJob, ...]] = (
    # 014-H2S unit 4087 / archive 3052 (#536): FINISH 2026-10-09 08:59 UTC, recorded completed.
    UnfedTailJob(
        printer_id=13,
        unit_id=4087,
        archive_id=3052,
        job_id="512918044",
        fed_extent=9,
        status_before="completed",
        status_after="failed",
        error_message_before=None,
        error_message_after="Printed without filament from layer 9 of 167",
        failure_reason_before=None,
        failure_reason_after=FAILURE_CATEGORY,
        archive_cost_before=5.23,
        archive_cost_after=1.23,
        log_entry_id=2435,
        log_grams_before=436.6,
        log_cost_before=5.2299999999999995,
        log_grams_after=103.0,
        log_cost_after=1.23,
        charges=(
            UnfedCharge(2641, 1184, 428.7, 5.14, 43, 95.1, 1.14, 10, 333.570227),
            UnfedCharge(2642, 1193, 7.9, 0.09, 1, 7.9, 0.09, 1, 0.0),
        ),
    ),
    # 011-H2S unit 4135 / archive 3056 (#538): FINISH 2026-10-09 23:44 UTC, recorded completed.
    UnfedTailJob(
        printer_id=9,
        unit_id=4135,
        archive_id=3056,
        job_id="515810211",
        fed_extent=93,
        status_before="completed",
        status_after="failed",
        error_message_before=None,
        error_message_after="Printed without filament from layer 93 of 167",
        failure_reason_before=None,
        failure_reason_after=FAILURE_CATEGORY,
        archive_cost_before=5.24,
        archive_cost_after=4.23,
        log_entry_id=2464,
        log_grams_before=436.6,
        log_cost_before=5.24,
        log_grams_after=352.4,
        log_cost_after=4.23,
        charges=(
            UnfedCharge(2676, 1165, 433.8, 5.21, 43, 349.6, 4.2, 35, 84.201287),
            UnfedCharge(2677, 1048, 2.8, 0.03, 0, 2.8, 0.03, 0, 0.0),
        ),
    ),
    # 014-H2S unit 4289 / archive 3104 (#558): the operator's stop 2026-10-10 08:08:36 UTC at
    # layer 142 of 167. ``cancelled`` is the true word and stays, as does its message; only the
    # grams change.
    UnfedTailJob(
        printer_id=13,
        unit_id=4289,
        archive_id=3104,
        job_id="593890429",
        fed_extent=134,
        status_before="cancelled",
        status_after="cancelled",
        error_message_before="[0300_0001] The front door is open.",
        error_message_after="[0300_0001] The front door is open.",
        failure_reason_before="User cancelled",
        failure_reason_after="User cancelled",
        archive_cost_before=5.24,
        archive_cost_after=4.93,
        log_entry_id=2495,
        log_grams_before=419.3,
        log_cost_before=5.03,
        log_grams_after=410.9,
        log_cost_after=4.93,
        charges=(
            UnfedCharge(2709, 1184, 360.9, 4.33, 36, 352.5, 4.23, 35, 8.304563),
            UnfedCharge(2710, 1193, 58.4, 0.7, 6, 58.4, 0.7, 6, 0.0),
        ),
    ),
)


@dataclass(slots=True)
class UnfedTailTally:
    """What one run of the repair did, and the lines it owes the log (written once applied)."""

    repaired: int = 0
    absent: int = 0
    drifted: int = 0
    credits_applied: int = 0
    credits_skipped: int = 0
    lines: list[tuple[int, str]] = field(default_factory=list)

    @property
    def wrote_or_refused(self) -> bool:
        """Did this run repair or refuse a job? A run over an install that holds none of the rows
        (a fresh one, a dev copy) did neither, and is not worth a warning."""
        return bool(self.repaired or self.drifted)

    def summary(self) -> str:
        return (
            f"{self.repaired} of {len(UNFED_TAIL_JOBS)} job(s) repaired, {self.absent} absent, {self.drifted} "
            f"skipped on drift; {self.credits_applied} spool credit(s) applied, {self.credits_skipped} skipped "
            "(one-time)"
        )


def _same(stored: float | None, expected: float | None) -> bool:
    if stored is None or expected is None:
        return stored is expected
    return abs(float(stored) - expected) < _EPSILON


def _expect(holds: bool, what: str) -> None:
    """A fact the report saw no longer holds: this job is not the repair's to write."""
    if not holds:
        raise RepairDrift(what)


async def _update_one(conn: AsyncConnection, sql: str, params: dict[str, object], what: str) -> None:
    """One guarded write that must hit exactly one row (its WHERE restates the verified pre-image)."""
    hit = (await conn.execute(text(sql), params)).rowcount
    if hit != 1:
        raise RepairDrift(f"{what} hit {hit} rows, expected 1")


async def repair_unfed_tail_20261010(conn: AsyncConnection) -> UnfedTailTally:
    """Apply ``UNFED_TAIL_JOBS``, one nested savepoint per job; the caller owns the outer one.

    Drift is decided PER JOB, unlike the replay repair, whose single plan applies whole or not at
    all. These literals were computed offline, hours before the deploy that carries them. The three
    jobs are independent facts: different units, archives and print-log rows. A job whose pre-image
    moved (an operator's edit, a later lane) can never match again, so it is skipped with its reason,
    and the others' over-charges still get repaired. An all-or-nothing abort would leave correctable
    damage in place because an unrelated row moved, and would fail the same way at every boot. Only
    an UNEXPECTED failure propagates, so the caller's savepoint discards everything and the next boot
    retries.

    A job is ``absent`` when neither its unit nor its archive exists: another install (a fresh one or
    a dev copy) that never held these rows. That is not drift.
    """
    tally = UnfedTailTally()
    for job in UNFED_TAIL_JOBS:
        # The job's own counts and lines, merged only once its savepoint has been released.
        job_tally = UnfedTailTally()
        try:
            async with conn.begin_nested():
                present = await _repair_unfed_tail_job(conn, job, job_tally)
        except RepairDrift as drift:
            tally.drifted += 1
            tally.lines.append(
                (
                    logging.WARNING,
                    f"skip job {job.job_id!r} (printer {job.printer_id}, unit {job.unit_id}, archive "
                    f"{job.archive_id}): {drift} -- nothing written for this job",
                )
            )
            continue
        if present:
            tally.repaired += 1
        else:
            tally.absent += 1
        tally.credits_applied += job_tally.credits_applied
        tally.credits_skipped += job_tally.credits_skipped
        tally.lines.extend(job_tally.lines)
    return tally


def _spool_refusal(spool: Row | None) -> str | None:
    """Why a spool's weight is not the repair's to move, or None when it is (the R-charge rule)."""
    if spool is None:
        return "the spool row is gone"
    if spool.weight_locked:
        return "the operator locked this spool's weight"
    if spool.spent_at is not None:
        return f"the spool was observed spent at {spool.spent_at}"
    if spool.archived_at is not None:
        return f"the spool was archived at {spool.archived_at}"
    return None


async def _repair_unfed_tail_job(conn: AsyncConnection, job: UnfedTailJob, tally: UnfedTailTally) -> bool:
    """Re-verify one job against the report's pre-image, then write it. False when it is absent.

    Raises ``RepairDrift`` on the first fact that no longer holds, before any write when the
    pre-image moved. The spools are the exception: they kept printing after the report, so their
    ``weight_used`` is never compared with a report figure. Each credit is a RELATIVE decrement of
    the value read here, floored at 0, guarded on that same read. A spool that is weight-locked, spent
    or archived is not the repair's to move (the replay repair's R-charge rule). Its credit is skipped,
    and its usage row is left exactly as charged so the spool's ledger still sums to its weight. The
    same rule lowers ``weight_used_baseline`` by the credit, floored at 0, when the credit takes
    ``weight_used`` below it: consumption since an operator's reset cannot be negative.
    """
    where = f"unit {job.unit_id} / archive {job.archive_id}"
    unit = (
        await conn.execute(
            text("SELECT status, printer_id, dispatch_subtask_id, error_message FROM print_queue WHERE id = :id"),
            {"id": job.unit_id},
        )
    ).first()
    archive = (
        await conn.execute(
            text("SELECT status, printer_id, subtask_id, failure_reason, cost FROM print_archives WHERE id = :id"),
            {"id": job.archive_id},
        )
    ).first()
    if unit is None and archive is None:
        tally.lines.append((logging.INFO, f"{where}: absent -- not the database this repair was computed from"))
        return False

    unit_seen = (job.status_before, job.printer_id, job.job_id, job.error_message_before)
    _expect(
        unit is not None and tuple(unit) == unit_seen,
        f"unit {job.unit_id} reads {tuple(unit) if unit is not None else 'absent'}, the report saw {unit_seen}",
    )
    archive_seen = (job.status_before, job.printer_id, job.job_id, job.failure_reason_before)
    _expect(
        archive is not None
        and (archive.status, archive.printer_id, archive.subtask_id, archive.failure_reason) == archive_seen
        and _same(archive.cost, job.archive_cost_before),
        f"archive {job.archive_id} reads {tuple(archive) if archive is not None else 'absent'}, the report saw "
        f"{(*archive_seen, job.archive_cost_before)}",
    )
    log_row = (
        await conn.execute(
            text(
                "SELECT archive_id, printer_id, status, filament_used_grams, cost, failure_reason "
                "FROM print_log_entries WHERE id = :id"
            ),
            {"id": job.log_entry_id},
        )
    ).first()
    _expect(
        log_row is not None
        and (log_row.archive_id, log_row.printer_id, log_row.status, log_row.failure_reason)
        == (job.archive_id, job.printer_id, job.status_before, job.failure_reason_before)
        and _same(log_row.filament_used_grams, job.log_grams_before)
        and _same(log_row.cost, job.log_cost_before),
        f"print-log entry {job.log_entry_id} reads {tuple(log_row) if log_row is not None else 'absent'}, the "
        f"report saw {(job.archive_id, job.printer_id, job.status_before, job.log_grams_before, job.log_cost_before)}",
    )
    for charge in job.charges:
        row = (
            await conn.execute(
                text(
                    "SELECT spool_id, archive_id, printer_id, status, weight_used, cost, percent_used "
                    "FROM spool_usage_history WHERE id = :id"
                ),
                {"id": charge.usage_id},
            )
        ).first()
        _expect(
            row is not None
            and (row.spool_id, row.archive_id, row.printer_id, row.status, row.percent_used)
            == (charge.spool_id, job.archive_id, job.printer_id, job.status_before, charge.percent_before)
            and _same(row.weight_used, charge.grams_before)
            and _same(row.cost, charge.cost_before),
            f"usage {charge.usage_id} reads {tuple(row) if row is not None else 'absent'}, the report saw "
            f"{(charge.spool_id, job.archive_id, job.printer_id, job.status_before, charge.grams_before, charge.cost_before, charge.percent_before)}",
        )

    # Every fact holds: write. Each statement's WHERE restates the pre-image it was verified on.
    if job.status_after != job.status_before:
        unit_params: dict[str, object] = {
            "after": job.status_after,
            "message": job.error_message_after,
            "id": job.unit_id,
            "before": job.status_before,
            "job": job.job_id,
        }
        # NULL is not equal to NULL in SQL: a NULL pre-image is restated as IS NULL.
        if job.error_message_before is None:
            message_guard = "error_message IS NULL"
        else:
            message_guard = "error_message = :message_before"
            unit_params["message_before"] = job.error_message_before
        await _update_one(
            conn,
            "UPDATE print_queue SET status = :after, error_message = :message WHERE id = :id AND status = :before "
            f"AND dispatch_subtask_id = :job AND {message_guard}",
            unit_params,
            f"UPDATE print_queue {job.unit_id}",
        )
        tally.lines.append(
            (
                logging.WARNING,
                f"unit {job.unit_id} (printer {job.printer_id}, job {job.job_id!r}): status {job.status_before!r} -> "
                f"{job.status_after!r}, error_message {job.error_message_before!r} -> {job.error_message_after!r}",
            )
        )
    else:
        tally.lines.append(
            (
                logging.INFO,
                f"unit {job.unit_id} (printer {job.printer_id}, job {job.job_id!r}): status {job.status_before!r} "
                "and its message kept; only the grams and costs change",
            )
        )
    await _update_one(
        conn,
        "UPDATE print_archives SET status = :after, failure_reason = :reason, cost = :cost "
        "WHERE id = :id AND status = :before AND subtask_id = :job AND ABS(cost - :cost_before) < :eps",
        {
            "after": job.status_after,
            "reason": job.failure_reason_after,
            "cost": job.archive_cost_after,
            "id": job.archive_id,
            "before": job.status_before,
            "job": job.job_id,
            "cost_before": job.archive_cost_before,
            "eps": _EPSILON,
        },
        f"UPDATE print_archives {job.archive_id}",
    )
    tally.lines.append(
        (
            logging.WARNING,
            f"archive {job.archive_id}: status {job.status_before!r} -> {job.status_after!r}, failure_reason "
            f"{job.failure_reason_before!r} -> {job.failure_reason_after!r}, cost {job.archive_cost_before} -> "
            f"{job.archive_cost_after} (filament_used_grams, the plate's slicer estimate, stays)",
        )
    )
    await _update_one(
        conn,
        "UPDATE print_log_entries SET status = :after, failure_reason = :reason, filament_used_grams = :grams, "
        "cost = :cost WHERE id = :id AND archive_id = :archive AND status = :before "
        "AND ABS(filament_used_grams - :grams_before) < :eps AND ABS(cost - :cost_before) < :eps",
        {
            "after": job.status_after,
            "reason": job.failure_reason_after,
            "grams": job.log_grams_after,
            "cost": job.log_cost_after,
            "id": job.log_entry_id,
            "archive": job.archive_id,
            "before": job.status_before,
            "grams_before": job.log_grams_before,
            "cost_before": job.log_cost_before,
            "eps": _EPSILON,
        },
        f"UPDATE print_log_entries {job.log_entry_id}",
    )
    tally.lines.append(
        (
            logging.WARNING,
            f"print-log entry {job.log_entry_id} (archive {job.archive_id}): status {job.status_before!r} -> "
            f"{job.status_after!r}, failure_reason {job.failure_reason_before!r} -> {job.failure_reason_after!r}, "
            f"filament_used_grams {job.log_grams_before} -> {job.log_grams_after}, cost {job.log_cost_before} -> "
            f"{job.log_cost_after}",
        )
    )

    for charge in job.charges:
        spool = None
        if charge.spool_credit_g > 0:
            spool = (
                await conn.execute(
                    text(
                        "SELECT weight_used, weight_used_baseline, weight_locked, spent_at, archived_at "
                        "FROM spool WHERE id = :id"
                    ),
                    {"id": charge.spool_id},
                )
            ).first()
            refusal = _spool_refusal(spool)
            if refusal is not None:
                tally.credits_skipped += 1
                tally.lines.append(
                    (
                        logging.WARNING,
                        f"skip spool {charge.spool_id} credit {charge.spool_credit_g} g: {refusal}; usage "
                        f"{charge.usage_id} (archive {job.archive_id}) is left as charged ({charge.grams_before} g)",
                    )
                )
                continue

        changed = (charge.grams_after, charge.cost_after, charge.percent_after, job.status_after) != (
            charge.grams_before,
            charge.cost_before,
            charge.percent_before,
            job.status_before,
        )
        if changed:
            await _update_one(
                conn,
                "UPDATE spool_usage_history SET weight_used = :grams, cost = :cost, percent_used = :percent, "
                "status = :after WHERE id = :id AND spool_id = :spool AND archive_id = :archive AND status = :before "
                "AND ABS(weight_used - :grams_before) < :eps",
                {
                    "grams": charge.grams_after,
                    "cost": charge.cost_after,
                    "percent": charge.percent_after,
                    "after": job.status_after,
                    "id": charge.usage_id,
                    "spool": charge.spool_id,
                    "archive": job.archive_id,
                    "before": job.status_before,
                    "grams_before": charge.grams_before,
                    "eps": _EPSILON,
                },
                f"UPDATE spool_usage_history {charge.usage_id}",
            )
            tally.lines.append(
                (
                    logging.WARNING,
                    f"usage {charge.usage_id} spool {charge.spool_id} (archive {job.archive_id}): weight_used "
                    f"{charge.grams_before} -> {charge.grams_after} g, cost {charge.cost_before} -> {charge.cost_after}, "
                    f"percent_used {charge.percent_before} -> {charge.percent_after}, status {job.status_before!r} -> "
                    f"{job.status_after!r}",
                )
            )
        else:
            tally.lines.append(
                (
                    logging.INFO,
                    f"usage {charge.usage_id} spool {charge.spool_id} (archive {job.archive_id}): unchanged "
                    f"({charge.grams_before} g owed = charged)",
                )
            )

        if spool is None:
            continue  # nothing to credit (a zero over-charge)
        used_before = float(spool.weight_used or 0.0)
        baseline_before = float(spool.weight_used_baseline or 0.0)
        used_after = max(0.0, used_before - charge.spool_credit_g)
        baseline_after = (
            max(0.0, baseline_before - charge.spool_credit_g) if used_after < baseline_before else baseline_before
        )
        await _update_one(
            conn,
            "UPDATE spool SET weight_used = :used_after, weight_used_baseline = :baseline_after "
            "WHERE id = :id AND ABS(COALESCE(weight_used, 0) - :used_before) < :eps "
            "AND ABS(COALESCE(weight_used_baseline, 0) - :baseline_before) < :eps "
            "AND COALESCE(weight_locked, :unlocked) = :unlocked AND spent_at IS NULL AND archived_at IS NULL",
            {
                "used_after": used_after,
                "baseline_after": baseline_after,
                "id": charge.spool_id,
                "used_before": used_before,
                "baseline_before": baseline_before,
                "unlocked": False,
                "eps": _EPSILON,
            },
            f"UPDATE spool {charge.spool_id}",
        )
        tally.credits_applied += 1
        baseline_note = (
            f"weight_used_baseline {baseline_before:.1f} -> {baseline_after:.1f} (a reset after the charge anchored it)"
            if baseline_after != baseline_before
            else f"weight_used_baseline {baseline_before:.1f} kept"
        )
        tally.lines.append(
            (
                logging.WARNING,
                f"spool {charge.spool_id} credited {charge.spool_credit_g} g for usage {charge.usage_id} (archive "
                f"{job.archive_id}): weight_used {used_before:.1f} -> {used_after:.1f}, {baseline_note}",
            )
        )
    return True
