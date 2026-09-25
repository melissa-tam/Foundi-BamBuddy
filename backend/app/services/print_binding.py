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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from sqlalchemy import func, or_, select, update

from backend.app.core.database import hold_write_lock
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.farm_correlation import resolve_printing_item

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

JobIdentity = Literal["same", "other", "unknown"]

# The printer's word for "this print names no job": Bambu reports "0" for a LAN / non-cloud print and
# an empty id on a screen restart. Neither identifies anything, on either side of a comparison.
_NO_JOB_IDS: frozenset[str] = frozenset({"", "0"})

# failure_reason of an archive closed because its printer started another job. Deliberately NOT
# prefixed "Stale": the #972 revive below reopens only a "Stale…" row, and a superseded print is over.
SUPERSEDED_REASON = "Superseded - the printer started another job"

# failure_reason of the id-less name resume's stale verdict (upstream #1485, unchanged). The #972
# revive keys on its "Stale" prefix.
_STALE_REASON = "Stale - print likely cancelled or failed without status update"

# The id-less name resume treats a matching ``printing`` archive as a stale leftover only when the
# printer reports near-zero progress AND the archive is older than this (upstream #1485, unchanged).
_NAME_RESUME_STALE_AGE_S = 2 * 60 * 60


def job_id(raw: object) -> str | None:
    """A subtask id normalised to the stripped string, or None when it names no job."""
    if raw is None:
        return None
    text = str(raw).strip()
    return None if text in _NO_JOB_IDS else text


def same_job(live: str | None, record: str | None) -> JobIdentity:
    """Are these two subtask ids the same print job?

    ``unknown`` when either side names no job (None, ``""`` or ``"0"``) — an absent id is not
    evidence of a DIFFERENT job, and callers decide what an unknown is worth to them (the terminal
    lookup accepts it, the adopt accepts it only for the sole printing unit). ``same`` / ``other``
    only when both sides name a job. THE one comparison of job identity; ``incident_resolution`` and
    ``dispatch_claim`` adopt it in a later wave.
    """
    live_id, record_id = job_id(live), job_id(record)
    if live_id is None or record_id is None:
        return "unknown"
    return "same" if live_id == record_id else "other"


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
    verdict = same_job(payload_subtask, archive.subtask_id)
    if verdict == "other":
        logger.info(
            "[PRINT-BINDING] printer %s: terminal job %r is not live archive %s's job %r — left for the reconcile",
            printer_id,
            payload_subtask,
            archive.id,
            archive.subtask_id,
        )
        return None
    return archive.id
