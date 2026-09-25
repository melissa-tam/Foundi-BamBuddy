"""THE owner of print ↔ archive binding: which ``print_archives`` row records the job a printer runs.

One archive row per print ATTEMPT, found by the job's IDENTITY — the printer's ``subtask_id``,
which for a farm dispatch is the id Bambuddy minted for it (``PrintQueueItem.dispatch_subtask_id``)
— and never by a name.

**Why this module exists (2026-09-25).** ``main`` used to find a print's archive at its terminal
through a process-memory dict keyed by ``(printer, name)``, plus an ``ILIKE`` name match; at print
START it matched the scheduler's dispatch through another name-keyed dict the scheduler registered
into. Both are deleted. Farm archives are named by the library STORAGE hash (``1d1054d9….3mf``) while the printer
echoes the human subtask name and ``/data/Metadata/plate_N.gcode``, so:

* after a restart nothing re-registered the echoed name, and every print in flight across the
  restart leaked its archive in ``printing`` forever (09-23: 6 of 6 prints spanning the 02:01 restart
  logged "Could not find archive for print complete") — while ``PrintArchive.subtask_id``, stamped at
  start and echoed by the terminal, was never read at completion;
* the start match missed whenever the printer normalised the name, so some prints got two archive
  rows and others overwrote their parent's.

Here the subtask id is the key, the database is the only memory, and a restart loses nothing.

**A unit's archive link is its DONOR, not its record.** ``PrintQueueItem.archive_id`` names the
bytes a unit prints FROM — a retry carries its parent's, a reprint the operator's — and since a row
that already recorded an attempt is never adopted again, it is not the unit's print record. The
record of a unit's attempt is reached only by identity: :func:`print_archive_of` (unit → record)
and :func:`unit_of_print_archive` (record → unit), ``archive.subtask_id ==
unit.dispatch_subtask_id`` on the unit's printer. ``test_code_quality.TestPrintRecordResolution``
allowlists the donor consumers of ``archive_id``.

**Invariant:** ``status='printing'`` and ``started_at`` on ``print_archives`` are written ONLY by this
module (``test_code_quality.TestArchiveBindingOwnership``; the one-shot migration repair in
``foreign_replay_repair`` and ``core.database`` excepted). Two things follow and both are load-bearing:

* ``started_at IS NULL`` means "this row was never bound to a print", exactly — which is what makes
  the adopt below a single atomic statement instead of a guess;
* the partial unique index ``ux_print_archives_live_printer`` (``models/archive.py``) holds at most one
  ``printing`` archive per printer, so "the printer's live archive" is a lookup, not a choice.

Every writer here that reads before it writes takes the SQLite write lock first
(``core.database.hold_write_lock``): under WAL a deferred transaction that read a snapshot cannot be
upgraded to a writer once another connection commits, and the adopt must see the latest commit.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select, update

from backend.app.core.database import hold_write_lock
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.farm_correlation import resolve_printing_item

# Re-exported: the job-identity comparison lives in the dependency-free ``job_identity`` so leaves
# (``dispatch_claim``, ``incident_resolution``) can take it; this module's callers keep one import.
from backend.app.services.job_identity import job_id, same_job

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# failure_reason of an archive closed because its printer started another job. Deliberately NOT
# prefixed "Stale": the #972 revive below reopens only a "Stale…" row, and a superseded print is over.
SUPERSEDED_REASON = "Superseded - the printer started another job"

# failure_reason of the id-less name resume's stale verdict (upstream #1485, unchanged). The #972
# revive keys on its "Stale" prefix.
_STALE_REASON = "Stale - print likely cancelled or failed without status update"

# The id-less name resume treats a matching ``printing`` archive as a stale leftover only when the
# printer reports near-zero progress AND the archive is older than this (upstream #1485, unchanged).
_NAME_RESUME_STALE_AGE_S = 2 * 60 * 60


@dataclass(frozen=True)
class LiveJob:
    """What the printer says it is running, read once when the print starts.

    ``subtask_id`` is the printer's echo, normalised by :func:`job_id`. The names and ``progress``
    feed only the id-less name resume (step 3 of :func:`attach`) — a job with an id is never
    matched by name.
    """

    subtask_id: str | None
    subtask_name: str | None = None
    filename: str | None = None
    progress: float | None = None


@dataclass(frozen=True)
class Resumed:
    """The job already has its archive (a duplicate start event, or a #972 stale-cancel revived)."""

    archive_id: int


@dataclass(frozen=True)
class Adopted:
    """The dispatched unit's never-printed archive is now this print's record."""

    archive_id: int
    unit_id: int


@dataclass(frozen=True)
class CreateNeeded:
    """No row can record this print — the caller creates one and hands it to :func:`bind_created`.

    ``unit_id`` names the dispatched unit when there is one (its durable dispatch id becomes the
    new row's ``subtask_id``); None for a print the farm did not dispatch.
    """

    unit_id: int | None


ArchiveBinding = Resumed | Adopted | CreateNeeded


async def dispatched_unit(db: AsyncSession, printer_id: int, live_id: str | None) -> PrintQueueItem | None:
    """The farm unit whose dispatch the printer is running, or None.

    ``farm_correlation.resolve_printing_item`` is the ONE in-flight attribution (the echoed id, else
    the sole ``printing`` unit), narrowed exactly as ``resolve_dispatch_donor`` narrows it: its
    sole-unit fallback also answers when the echo names a DIFFERENT job, and a job the printer
    identifies as another one — a screen start on a printer whose unit has not gone terminal — is
    not that unit's print, whatever else is claimed on the printer.
    """
    unit = await resolve_printing_item(db, printer_id, live_id)
    if unit is None or same_job(live_id, unit.dispatch_subtask_id) == "other":
        return None
    return unit


async def adoptable_dispatch(db: AsyncSession, printer_id: int, live_job: LiveJob) -> PrintQueueItem | None:
    """The dispatched unit whose archive :func:`attach` would adopt for this job, or None.

    A read, for a caller that must decide BEFORE binding whether the farm owns a record of this
    print — ``main.on_print_start`` with auto-archive off records only the farm's own dispatches.
    """
    unit = await dispatched_unit(db, printer_id, live_job.subtask_id)
    if unit is None or unit.archive_id is None:
        return None
    adoptable = await db.scalar(select(PrintArchive.id).where(PrintArchive.id == unit.archive_id, *_never_bound()))
    return unit if adoptable is not None else None


async def live_print_archive(db: AsyncSession, printer_id: int) -> PrintArchive | None:
    """The printer's ``printing`` archive — at most one, by ``ux_print_archives_live_printer``."""
    return (
        (
            await db.execute(
                select(PrintArchive)
                .where(PrintArchive.printer_id == printer_id)
                .where(PrintArchive.status == "printing")
                .order_by(PrintArchive.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def printers_with_live_print(db: AsyncSession) -> set[int]:
    """Every printer that has a ``printing`` archive."""
    rows = await db.execute(
        select(PrintArchive.printer_id)
        .where(PrintArchive.status == "printing")
        .where(PrintArchive.printer_id.is_not(None))
        .distinct()
    )
    return {printer_id for (printer_id,) in rows.all()}


async def count_live_prints(db: AsyncSession) -> int:
    """How many archives are ``printing``, fleet-wide."""
    return int(await db.scalar(select(func.count(PrintArchive.id)).where(PrintArchive.status == "printing")) or 0)


def is_live_status(status: str | None) -> bool:
    """Does an archive ``status`` value say "a printer is running the print this row records"?

    The ONE spelling for readers outside this module: only :func:`attach` / :func:`bind_created`
    make a row live and only :func:`close_archive` ends it, so an edit surface asks this of the row
    it would touch and of the value it would write (``PATCH /archives/{id}``).
    """
    return status == "printing"


def live_print_clause() -> ColumnElement[bool]:
    """:func:`is_live_status` as a WHERE clause over ``print_archives`` — what a deletion keeps."""
    return PrintArchive.status == "printing"


def _records_unit() -> ColumnElement[bool]:
    """The identity join "this archive records that unit's print": the unit's durable dispatch id is
    the archive's job id, on the same printer. Both columns are stored normalised (the dispatcher
    mints ids >= 1 and the binding stamps :func:`job_id`'s value), so SQL equality is the identity
    and a NULL on either side matches nothing. ONE spelling, for :func:`unit_of_print_archive` and
    :func:`uncounted_live_records`."""
    return (PrintArchive.printer_id == PrintQueueItem.printer_id) & (
        PrintArchive.subtask_id == PrintQueueItem.dispatch_subtask_id
    )


async def uncounted_live_records(
    db: AsyncSession, scope: ColumnElement[bool], counted_units: Collection[int]
) -> list[int]:
    """The LIVE print records ``scope`` selects whose print is not one of ``counted_units``.

    The deletion guards ask it after counting the printing units that print FROM what they delete
    (the donor link, ``queue_transitions.live_prints_blocking``). Under one archive per attempt a
    live record is usually no unit's ``archive_id`` — a retry prints into a NEW row — so the donor
    count never sees it, and deleting it mid-print purges the files the print's charge and finish
    photo still need and leaves its terminal nothing to close. Counted ONCE: a first attempt adopts
    its own dispatch copy, so there the live record is one of the counted units' print. A live
    record whose unit cannot be named (an id-less or foreign print) always counts.
    """
    rows = await db.execute(
        select(PrintArchive.id, PrintQueueItem.id)
        .select_from(PrintArchive)
        .outerjoin(PrintQueueItem, _records_unit())
        .where(scope, live_print_clause())
    )
    counted = set(counted_units)
    already_counted: dict[int, bool] = {}
    for archive_id, unit_id in rows.all():
        already_counted[archive_id] = already_counted.get(archive_id, False) or unit_id in counted
    return sorted(archive_id for archive_id, seen in already_counted.items() if not seen)


def _records_job(archive: PrintArchive, job: str | None) -> bool:
    """Is ``archive`` the record of ``job``? Anything but a positive ``other``: an id-less print, or
    an echo that has not arrived, is the printer's one live print. ONE rule, for
    :func:`live_archive_for_job` and :func:`resolve_terminal`."""
    return same_job(job, archive.subtask_id) != "other"


async def live_archive_for_job(db: AsyncSession, printer_id: int, job: str | None) -> PrintArchive | None:
    """The printer's live archive when it records ``job`` (the printer's live echo), else None.

    ``other`` means the live row is a print whose terminal the farm never saw — the downtime
    reconcile's to close — and showing it for the running job would label one print with another's
    record.
    """
    archive = await live_print_archive(db, printer_id)
    if archive is None or not _records_job(archive, job):
        return None
    return archive


async def print_archive_of(db: AsyncSession, unit: PrintQueueItem) -> PrintArchive | None:
    """THE unit → attempt-record resolver: the archive that records this unit's print, or None.

    ``unit.archive_id`` is the unit's DONOR — the bytes it prints from, which for a failure retry, a
    refused-plate requeue or a fault-stop requeue is the PARENT's printed record — so it is never
    read as the unit's own print. The record is the archive on the unit's printer whose
    ``subtask_id`` is the unit's durable ``dispatch_subtask_id`` (what :func:`attach` /
    :func:`bind_created` stamp), newest first. None when the unit never dispatched (no id), has no
    printer, or its print has not been bound yet.
    """
    job = job_id(unit.dispatch_subtask_id)
    if job is None or unit.printer_id is None:
        return None
    return (
        (
            await db.execute(
                select(PrintArchive)
                .where(PrintArchive.printer_id == unit.printer_id)
                .where(PrintArchive.subtask_id == job)
                .order_by(PrintArchive.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def unit_of_print_archive(
    db: AsyncSession, archive_id: int, *, statuses: Collection[str] | None = None
) -> PrintQueueItem | None:
    """THE record → unit resolver: the farm unit whose dispatch this archive records, or None.

    The inverse of :func:`print_archive_of` — ``unit.dispatch_subtask_id == archive.subtask_id`` on
    the archive's printer (:func:`_records_unit`) — in ONE statement. ``statuses`` narrows to the
    caller's lifecycle window (a completion accepts the terminal statuses, a print start only
    ``printing``). None for a print the farm did not dispatch. A dispatch id names one unit, so
    ``limit(1)`` reads the one row.
    """
    stmt = select(PrintQueueItem).join(PrintArchive, _records_unit()).where(PrintArchive.id == archive_id)
    if statuses is not None:
        stmt = stmt.where(PrintQueueItem.status.in_(tuple(statuses)))
    return (await db.execute(stmt.order_by(PrintQueueItem.id.desc()).limit(1))).scalar_one_or_none()


async def close_archive(
    db: AsyncSession,
    archive_id: int,
    *,
    status: str,
    completed_at: datetime | None,
    failure_reason: str | None = None,
) -> bool:
    """THE ``printing`` → terminal writer. True when THIS call closed the row.

    Conditional on ``status='printing'`` in one statement, so two closers racing over one archive (a
    reconcile synthesis and the real terminal) close it once and the loser learns it lost instead of
    rewriting a finished row. ``completed_at`` / ``failure_reason`` are written only when given, as
    the upstream status update did — the stale-cancel below records no completion time.

    Does not commit: the caller owns the transaction, and a supersede must land in the same one as
    the binding that caused it.
    """
    if status == "printing":
        raise ValueError("close_archive ends a live print; binding one is attach / bind_created")
    values: dict[str, object] = {"status": status}
    if completed_at is not None:
        values["completed_at"] = completed_at
    if failure_reason:
        values["failure_reason"] = failure_reason
    closed = await db.execute(
        update(PrintArchive)
        .where(PrintArchive.id == archive_id)
        .where(PrintArchive.status == "printing")
        .values(**values)
        .returning(PrintArchive.id)
        .execution_options(synchronize_session=False)
    )
    return closed.scalar_one_or_none() is not None


async def supersede_other_live(
    db: AsyncSession,
    printer_id: int,
    keep_archive_id: int,
    *,
    now: datetime | None = None,
) -> list[int]:
    """Close every OTHER ``printing`` archive on the printer as ``cancelled``; return their ids.

    A printer runs one job, so a second live archive on it cannot be this job's record: it is a
    print whose terminal the farm never saw. It must be closed BEFORE this job's row turns
    ``printing`` — the partial unique index would refuse the binding otherwise — and in the same
    transaction, so no reader ever sees the printer with two live prints or none.

    A later wave routes this through the terminal's JOB PHASE (the unit's disposition, the missing
    print-log row) instead of a bare close; today the outcome is recorded unknown and nothing else is
    written. Does not commit.
    """
    stale = (
        await db.execute(
            select(PrintArchive.id, PrintArchive.subtask_id)
            .where(PrintArchive.printer_id == printer_id)
            .where(PrintArchive.status == "printing")
            .where(PrintArchive.id != keep_archive_id)
        )
    ).all()
    closed: list[int] = []
    for other_id, other_job in stale:
        if await close_archive(
            db,
            other_id,
            status="cancelled",
            completed_at=now or datetime.now(timezone.utc),
            failure_reason=SUPERSEDED_REASON,
        ):
            closed.append(other_id)
            logger.warning(
                "[PRINT-BINDING] printer %s: archive %s (job %r) was still printing when archive %s became the "
                "live print — closed cancelled, outcome unknown",
                printer_id,
                other_id,
                other_job,
                keep_archive_id,
            )
    return closed


def _never_bound() -> tuple[ColumnElement[bool], ...]:
    """The predicate "this archive never recorded a print" — ONE spelling, for the adopt and its pre-check.

    ``started_at IS NULL`` is exact because this module is the only writer of ``started_at``; the
    status and soft-delete clauses keep a live row and a deleted one out regardless.
    """
    return (
        PrintArchive.started_at.is_(None),
        PrintArchive.status != "printing",
        PrintArchive.deleted_at.is_(None),
    )


async def _bind_never_printed(
    db: AsyncSession,
    archive_id: int,
    *,
    printer_id: int,
    job: str | None,
    now: datetime,
) -> bool:
    """Make a never-printed archive the printer's live print, in one atomic statement.

    The WHERE is the precondition (:func:`_never_bound`), so two printers starting copies of ONE
    uploaded archive cannot both take it — the database evaluates the precondition and the write
    together, and the loser's ``RETURNING`` is empty.
    """
    bound = await db.execute(
        update(PrintArchive)
        .where(PrintArchive.id == archive_id, *_never_bound())
        .values(status="printing", started_at=now, completed_at=None, subtask_id=job, printer_id=printer_id)
        .returning(PrintArchive.id)
        .execution_options(synchronize_session=False)
    )
    return bound.scalar_one_or_none() is not None


async def _resume_by_job(db: AsyncSession, printer_id: int, job: str, *, now: datetime) -> int | None:
    """Step 1 of :func:`attach`: the job's own archive, by id — its live one, or a #972 revive."""
    live = (
        (
            await db.execute(
                select(PrintArchive.id)
                .where(PrintArchive.printer_id == printer_id)
                .where(PrintArchive.subtask_id == job)
                .where(PrintArchive.status == "printing")
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if live is not None:
        await supersede_other_live(db, printer_id, live, now=now)
        logger.info("[PRINT-BINDING] printer %s job %s: resuming archive %s", printer_id, job, live)
        return live

    # #972: an earlier build stale-cancelled the live row of a print that was still running. The
    # same subtask id reappearing proves it is the same print, so the row is revived rather than a
    # second one created. Only a "Stale…" cancel — any other cancel is a print that really ended.
    stale = (
        (
            await db.execute(
                select(PrintArchive.id)
                .where(PrintArchive.printer_id == printer_id)
                .where(PrintArchive.subtask_id == job)
                .where(PrintArchive.status == "cancelled")
                .where(PrintArchive.failure_reason.like("Stale%"))
                .order_by(PrintArchive.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if stale is None:
        return None
    await supersede_other_live(db, printer_id, stale, now=now)
    revived = await db.execute(
        update(PrintArchive)
        .where(PrintArchive.id == stale)
        .where(PrintArchive.status == "cancelled")
        .values(status="printing", failure_reason=None, completed_at=None)
        .returning(PrintArchive.id)
        .execution_options(synchronize_session=False)
    )
    if revived.scalar_one_or_none() is None:
        return None
    logger.warning(
        "[PRINT-BINDING] printer %s: reviving stale-cancelled archive %s — matching subtask_id %s confirms "
        "the same print (#972)",
        printer_id,
        stale,
        job,
    )
    return stale


async def _resume_by_name(db: AsyncSession, printer_id: int, live_job: LiveJob, *, now: datetime) -> int | None:
    """Step 3 of :func:`attach`, for a job that names no id: upstream's name resume, unchanged.

    A ``printing`` archive whose name matches is this print — unless the printer shows a different,
    freshly started print: near-0 % progress on an archive far too old to still be at 0 % (#1485).
    Unknown progress never cancels; resuming is the safe default. The stale one is closed
    ``cancelled`` and the caller creates a new record.
    """
    check_name = live_job.subtask_name or (live_job.filename or "").split("/")[-1].replace(".gcode", "").replace(
        ".3mf", ""
    )
    if not check_name:
        return None
    candidate = (
        (
            await db.execute(
                select(PrintArchive)
                .where(PrintArchive.printer_id == printer_id)
                .where(PrintArchive.status == "printing")
                .where(
                    or_(
                        PrintArchive.print_name == check_name,
                        PrintArchive.filename.in_([f"{check_name}.3mf", f"{check_name}.gcode.3mf"]),
                    )
                )
                .order_by(PrintArchive.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if candidate is None:
        return None

    created_at = candidate.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    archive_age = now - created_at
    progress = live_job.progress
    if progress is not None and progress < 1.0 and archive_age.total_seconds() > _NAME_RESUME_STALE_AGE_S:
        logger.warning(
            "[PRINT-BINDING] printer %s: stale 'printing' archive %s (age %s, printer progress %.0f%%) — "
            "closing it cancelled and creating a new record",
            printer_id,
            candidate.id,
            archive_age,
            progress,
        )
        await close_archive(db, candidate.id, status="cancelled", completed_at=None, failure_reason=_STALE_REASON)
        return None

    await supersede_other_live(db, printer_id, candidate.id, now=now)
    logger.info(
        "[PRINT-BINDING] printer %s: resuming id-less print on archive %s by name %r",
        printer_id,
        candidate.id,
        check_name,
    )
    return candidate.id


async def attach(db: AsyncSession, printer_id: int, live_job: LiveJob, *, now: datetime) -> ArchiveBinding:
    """Which archive records the job that just started on ``printer_id`` — THE print-start binding.

    The job's identity is the dispatched unit's DURABLE ``dispatch_subtask_id`` when a unit is
    resolved (:func:`dispatched_unit`), else the printer's echo. Never ``client
    .last_dispatch_subtask_id`` (process memory), and never the echo over the unit's own id: the echo
    may not have arrived yet, and the unit's id is the one its terminal will carry.

    Decision table, first match wins:

    ====  ===================================================================  ==================
    step  when                                                                 outcome
    ====  ===================================================================  ==================
    1     a ``printing`` archive on this printer carries the job id             ``Resumed``
    1b    a "Stale…"-cancelled archive on this printer carries it (#972)        revived, ``Resumed``
    2     a dispatched unit whose archive was never bound to a print            ``Adopted``
    3     NO job id at all: a ``printing`` archive whose name matches and is    ``Resumed``
          not a stale leftover (a stale one is closed cancelled first)
    4     otherwise                                                             ``CreateNeeded``
    ====  ===================================================================  ==================

    The adopt (2) is one statement — :func:`_bind_never_printed` — so a unit whose archive already
    recorded an attempt (a retry carries its parent's, a reprint the operator's) gets a NEW row: one
    archive per attempt. Before ``Resumed`` / ``Adopted`` every OTHER ``printing`` archive on the
    printer is superseded (:func:`supersede_other_live`). Commits: the binding is one unit of work,
    and the caller's side effects that follow must not run under the write lock.
    """
    await hold_write_lock(db)
    unit = await dispatched_unit(db, printer_id, live_job.subtask_id)
    job = job_id(unit.dispatch_subtask_id if unit is not None else None) or live_job.subtask_id

    binding: ArchiveBinding
    resumed = await _resume_by_job(db, printer_id, job, now=now) if job is not None else None
    if resumed is not None:
        binding = Resumed(resumed)
    elif unit is not None and unit.archive_id is not None and await _adopt(db, printer_id, unit, job=job, now=now):
        binding = Adopted(unit.archive_id, unit.id)
    elif job is None and (by_name := await _resume_by_name(db, printer_id, live_job, now=now)) is not None:
        binding = Resumed(by_name)
    else:
        binding = CreateNeeded(unit.id if unit is not None else None)
    await db.commit()
    return binding


async def _adopt(db: AsyncSession, printer_id: int, unit: PrintQueueItem, *, job: str | None, now: datetime) -> bool:
    """Step 2 of :func:`attach`: bind the unit's archive if it never recorded a print."""
    archive_id = unit.archive_id
    if archive_id is None:
        return False
    # Before the bind: the index admits one live print per printer, and any other one here is a
    # print that ended unseen. Whether or not the adopt wins, it is not this job's record.
    await supersede_other_live(db, printer_id, archive_id, now=now)
    if await _bind_never_printed(db, archive_id, printer_id=printer_id, job=job, now=now):
        logger.info(
            "[PRINT-BINDING] printer %s job %s: adopted unit %s's archive %s", printer_id, job, unit.id, archive_id
        )
        return True
    logger.info(
        "[PRINT-BINDING] printer %s job %s: unit %s's archive %s already recorded a print — a new record is "
        "needed (one archive per attempt)",
        printer_id,
        job,
        unit.id,
        archive_id,
    )
    return False


async def bind_created(
    db: AsyncSession,
    archive_id: int,
    printer_id: int,
    live_job: LiveJob,
    unit_id: int | None,
    *,
    now: datetime,
) -> bool:
    """Make the row the caller just created (:class:`CreateNeeded`) the printer's live print.

    The create branch builds its row NON-printing — the 3MF download through
    ``ArchiveService.archive_print``, or the no-3MF fallback archive — and binds it here, so
    ``status='printing'`` and ``started_at`` keep one writer. The row's job id is the unit's durable
    ``dispatch_subtask_id`` when a unit dispatched this print, else the printer's echo. Every other
    ``printing`` archive on the printer is superseded in the same transaction. Commits; True when the
    row was bound (False only if it was already bound — a caller bug, logged).
    """
    await hold_write_lock(db)
    job = live_job.subtask_id
    if unit_id is not None:
        unit = await db.get(PrintQueueItem, unit_id)
        if unit is not None:
            job = job_id(unit.dispatch_subtask_id) or job
    await supersede_other_live(db, printer_id, archive_id, now=now)
    bound = await _bind_never_printed(db, archive_id, printer_id=printer_id, job=job, now=now)
    await db.commit()
    if bound:
        logger.info("[PRINT-BINDING] printer %s job %s: new archive %s is the live print", printer_id, job, archive_id)
    else:
        logger.warning(
            "[PRINT-BINDING] printer %s job %s: archive %s was already bound to a print — not rebound",
            printer_id,
            job,
            archive_id,
        )
    return bound


async def resolve_terminal(db: AsyncSession, printer_id: int, payload_subtask: str | None) -> int | None:
    """The archive a terminal on ``printer_id`` closes, or None.

    The printer's one ``printing`` archive when its job is the terminal's (``same``) or either side
    names no job (``unknown`` — an id-less print, or a terminal the firmware reset). ``other`` means
    that archive is a print whose own terminal the farm never saw, and THIS terminal is somebody
    else's: returning it would close the wrong record (the 2026-09 replays closed the CURRENT
    print's archive from a stale one's names). The downtime reconcile owns a stale archive.
    """
    archive = await live_print_archive(db, printer_id)
    if archive is None:
        return None
    if not _records_job(archive, payload_subtask):
        logger.info(
            "[PRINT-BINDING] printer %s: terminal job %r is not live archive %s's job %r — left for the reconcile",
            printer_id,
            payload_subtask,
            archive.id,
            archive.subtask_id,
        )
        return None
    return archive.id
