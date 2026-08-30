"""Deleting a user — the one place that transition is expressed.

``DELETE /users/{id}`` was, until this module existed, a controller holding a
short list of raw multi-table DELETE statements. Three things follow from that
shape, and all three were live in production:

**A live print loses its backing artefact.** The route's guard asked "is any
queue item THIS USER created printing?" while its DELETEs asked "does any queue
item reference THIS USER's rows?" — different sets, and routinely so.
``SkuFile`` carries no owner at all, and ``production_run.create_production_run``
builds queue items with ``library_file_id`` from the SKU catalog but
``created_by_id`` from whoever STARTED the run; the queue route explicitly
permits cross-user queueing under ``LIBRARY_READ_ALL``. So operator B's live
print habitually runs off user A's library file, and deleting A severed it. On
SQLite the row simply dangled (``core.database`` enables WAL, ``busy_timeout``
and ``synchronous`` — never ``foreign_keys``, so no FK is enforced at all); on
Postgres the declared ``ON DELETE CASCADE`` on ``print_queue.archive_id`` /
``library_file_id`` deleted the printing row outright. That is the 2026-08-22
incident (run 114: three units hard-deleted mid-print, roughly 27 printer-hours
idle across 001/002/003-H2S) reproduced across users, where no amount of care by
the deleting admin can see it coming.

**Every byte was orphaned.** The raw DELETEs bypassed ``ArchiveService`` and
``LibraryTrashService``, the only two components that remove files from disk, so
the rows went and the bytes stayed. Nothing ever collects them: the library trash
sweeper walks ROWS to find bytes, and the rows are gone. An orphan created this
way is permanent and invisible.

**The confirm dialog lied.** ``/users/{id}/items-count`` filtered
``LibraryFile.deleted_at IS NULL``, so it under-counted exactly what the delete
destroyed, and said nothing at all about SKUs or running prints.

So the module owns BOTH branches of ``delete_items``, and is a module of
functions rather than a class for the same reason ``queue_transitions`` and
``spool_binding`` are: there is no per-instance state, and a class would only
offer somewhere for a second, subtly different deletion path to grow.

**The shape of the destructive branch.** One transaction, four scopes (the
user's queue items, then their archives, library files and batches), every
parent delete carrying ``queue_transitions.printing_items_referencing`` in its
own WHERE so the database evaluates "is a print running off this row?" and
performs the delete as ONE statement. Blockers are then read back across all
four scopes and ANY of them refuses the WHOLE request with a single 409 naming
every printer — not the first one found, because an admin who stops one printer
and retries only to be refused again learns nothing about how much is left.
There is deliberately no partial outcome.

**Why the FK cascades are performed by hand.** SQLite enforces nothing, and the
farm's production database is SQLite. Every ``ondelete`` in the schema is
therefore a statement of INTENT that some code has to carry out; where this
module issues an UPDATE-to-NULL or a DELETE for a dependent row, it is running
the policy the model already declares. On Postgres the engine has already done
it and these statements are no-ops. The one place the two disagreed —
``spool_usage_history.archive_id``, which declared no ``ondelete`` at all and so
defaulted to NO ACTION — is now declared ``SET NULL`` to match what runs here:
the gram ledger is the authoritative record of what was consumed and must
outlive the archive it was derived from.

That "SQLite enforces nothing" is measured, not assumed. ``PRAGMA
foreign_key_check`` on a backup-API copy of the live farm database (2026-08-23)
returned **282 violations** across seven foreign keys: 126
``print_queue.library_file_id``, 51 ``print_log_entries.created_by_id``, 51
``print_log_entries.archive_id``, 27 ``spool_usage_history.archive_id``, 23
``print_batches.library_file_id``, 3 ``spool_usage_history.spool_id`` and 1
``sponsor_toast_state.user_id``. Every one of those columns that still exists is
a row in the declaration tables below (``sponsor_toast_state`` was dropped with
the sponsor toast, 2026-08-30 — it was per-user nag state, and its single
dangling row went with the table). So the dangling references this module
prevents are not hypothetical; they are the accumulated residue of the raw
multi-table DELETEs it replaces. ``sku_files`` is clean at 0, which is what the
CASCADE looks like when nothing has yet deleted a library file out from under a
SKU.

Turning the enforcement ON (``PRAGMA foreign_keys = ON``) is deliberately NOT
part of this change: with 282 standing violations the flip would start failing
ordinary writes on contact. It needs a repair migration first, and this module
is the prerequisite for that — it stops the bleeding, so a repair has a fixed
point to converge on rather than a moving one.

**Ordering inside the transaction is load-bearing.** Dependents are handled in
two phases around the blocker read, split by ONE rule: anything that touches
``print_queue`` waits until after the refusal has been decided. The queue rows
ARE the evidence — ``delete_related_queue_items`` deletes them regardless of
status, and NULLing ``batch_id`` erases the link the batch scope is guarded by —
so running either before the read would destroy the very rows that were supposed
to refuse the delete, and the request would succeed silently. Everything else
(the SET NULLs and the non-queue cascades) runs BEFORE the parent deletes,
because on Postgres a NO-ACTION FK aborts the parent DELETE outright.

**Bytes go last, after the commit.** Paths are resolved while the rows are still
loaded (the row carries the path), the transaction commits, and only then does
``archive.purge_paths`` run. A commit that loses SQLite's single write lock must
leave the files where they are: a row without its bytes is a visible 404 the
operator can act on, while bytes without a row are the permanent orphan above.
On the refusal path nothing is purged at all.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeVar

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select, update

from backend.app.models.active_print_spoolman import ActivePrintSpoolman
from backend.app.models.api_key import APIKey
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile, LibraryFileTag, LibraryFolder
from backend.app.models.long_lived_token import LongLivedToken
from backend.app.models.oidc_provider import UserOIDCLink
from backend.app.models.pending_upload import PendingUpload
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_log import PrintLogEntry
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.project_bom import ProjectBOMItem
from backend.app.models.sku import SkuFile
from backend.app.models.slot_recheck import SlotRecheckIntent
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.models.user import User
from backend.app.models.user_otp_code import UserOTPCode
from backend.app.models.user_totp import UserTOTP
from backend.app.schemas.auth import UserDeleteImpact
from backend.app.services.archive import (
    delete_related_queue_items,
    null_print_log_thumbnail_paths,
    purge_paths,
    resolve_archive_dir_for_delete,
)
from backend.app.services.library_trash import owned_paths
from backend.app.services.queue_transitions import (
    delete_user_items_unless_printing,
    live_prints_blocking,
    printing_items_referencing,
    printing_units_conflict,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import InstrumentedAttribute
    from sqlalchemy.sql.expression import ScalarSelect

logger = logging.getLogger(__name__)

# The three tables that BACK a queue item and carry an owner: what
# ``delete_items=true`` destroys, and what a live print can be running off. Named
# once so the guarded delete, the id subquery and the counts cannot drift apart
# about which tables this module is allowed to touch.
_BackingRow = TypeVar("_BackingRow", PrintArchive, LibraryFile, PrintBatch)

# The structured-refusal vocabulary the frontend already renders. Unchanged from
# when the 2026-08-22 guard lived in the route: the i18n key is keyed off it.
_CONFLICT_CODE = "user_has_printing_units"
_CONFLICT_SUBJECT = "This user"


@dataclass(frozen=True)
class _DeclaredFk:
    """One foreign key whose declared ``ondelete`` no engine in this fork runs.

    A row of the schema, restated where it can actually be executed. *column* is
    the CHILD column pointing at the parent being deleted; *policy* is verbatim
    what the model's ``ForeignKey(..., ondelete=...)`` says, so a reviewer can
    diff this table against the models without translating between two
    vocabularies. Adding an FK to a model and not to this table is the bug this
    shape exists to make visible.
    """

    column: InstrumentedAttribute[int] | InstrumentedAttribute[int | None]
    policy: Literal["CASCADE", "SET NULL"]


# Dependents of an archive, EXCEPT ``print_queue.archive_id`` — that one is the
# blocker evidence, so it runs after the refusal is decided, through
# ``archive.delete_related_queue_items`` (which carries the #1734 doctrine on
# why terminal queue rows go while PrintLogEntry stays).
_ARCHIVE_DEPENDENTS: tuple[_DeclaredFk, ...] = (
    _DeclaredFk(ActivePrintSpoolman.archive_id, "CASCADE"),
    _DeclaredFk(PrintLogEntry.archive_id, "SET NULL"),
    # The gram ledger outlives what it was derived from — see the module
    # docstring on why this one's declaration had to change.
    _DeclaredFk(SpoolUsageHistory.archive_id, "SET NULL"),
    _DeclaredFk(PrintBatch.archive_id, "SET NULL"),
    _DeclaredFk(LibraryFolder.archive_id, "SET NULL"),
    _DeclaredFk(PendingUpload.archived_id, "SET NULL"),
    _DeclaredFk(ProjectBOMItem.archive_id, "SET NULL"),
)

# Dependents of a library file, again excepting ``print_queue.library_file_id``.
# ``sku_files.library_file_id`` is CASCADE and NOT NULL, so the SKU's link to
# this file cannot survive it; the SkuFile rows that go take their own dependent
# with them (below).
_LIBRARY_FILE_DEPENDENTS: tuple[_DeclaredFk, ...] = (
    _DeclaredFk(LibraryFileTag.file_id, "CASCADE"),
    _DeclaredFk(SkuFile.library_file_id, "CASCADE"),
    _DeclaredFk(PrintBatch.library_file_id, "SET NULL"),
)

# Second order: a run points at the SkuFile the line above just destroyed.
_SKU_FILE_DEPENDENTS: tuple[_DeclaredFk, ...] = (_DeclaredFk(PrintBatch.sku_file_id, "SET NULL"),)

# A batch's only dependent, and the reason the phase split exists: this UPDATE
# erases the very link the batch scope is GUARDED by, so unlike every table
# above it, it must not run until the refusal has been decided.
_BATCH_DEPENDENTS: tuple[_DeclaredFk, ...] = (_DeclaredFk(PrintQueueItem.batch_id, "SET NULL"),)


async def delete_user(db: AsyncSession, *, user: User, delete_items: bool) -> None:
    """Delete *user*, and either destroy or disown everything they created.

    The route has already decided that this user MAY be deleted (they exist, they
    are not the last admin, they are not the caller). Everything from here — the
    two ``delete_items`` branches, the user-aggregate rows, the row itself, the
    commit and the bytes — is one transition and lives here.

    Raises the structured 409 ``user_has_printing_units`` when the destructive
    branch would sever a live print, having rolled the whole transaction back
    first: the DELETEs have already run against it, so merely raising would leave
    them to be committed by whatever touches that session next.
    """
    user_id = user.id
    doomed: list[Path] = []

    if delete_items:
        doomed = await _destroy_owned_items(db, user_id)
    else:
        await _disown_owned_items(db, user_id)

    await _delete_user_aggregate_rows(db, user_id)
    await db.delete(user)
    await db.commit()

    purge_paths(doomed)


async def delete_impact(db: AsyncSession, *, user_id: int) -> UserDeleteImpact:
    """What ``delete_items=true`` would do to this user's estate, counted now.

    A pre-flight for the delete-confirm dialog, modelled on
    ``GET /archives/{id}/delete-impact``: one cheap endpoint rather than a per-row
    cost on the much larger user list. Every count is a plain SELECT against the
    same scopes :func:`_destroy_owned_items` deletes by, so the dialog and the
    delete cannot describe different estates.

    ``library_files`` deliberately does NOT filter ``deleted_at`` — the delete
    destroys trashed rows too, and the old ``items-count`` endpoint's filter was
    the specific reason the dialog under-reported. ``currently_printing`` is the
    refusal forecast: ``live_prints_blocking`` over all four scopes at once, the
    same question the guard answers, so a dialog that shows zero and a delete that
    409s cannot disagree about what ``printing`` means.

    It IS a forecast and not a guarantee. A unit that dispatches between this call
    and the delete makes the delete refuse anyway — which is the correct outcome
    and the whole reason the guard lives in the DELETE's own WHERE rather than in
    a check like this one.
    """
    archive_ids = _owned_ids(PrintArchive, user_id)
    library_file_ids = _owned_ids(LibraryFile, user_id)
    batch_ids = _owned_ids(PrintBatch, user_id)

    return UserDeleteImpact(
        archives=await _count(db, PrintArchive, PrintArchive.created_by_id == user_id),
        library_files=await _count(db, LibraryFile, LibraryFile.created_by_id == user_id),
        queue_items=await _count(db, PrintQueueItem, PrintQueueItem.created_by_id == user_id),
        production_runs=await _count(db, PrintBatch, PrintBatch.created_by_id == user_id),
        dependent_skus=await _count_distinct(db, SkuFile.sku_id, SkuFile.library_file_id.in_(library_file_ids)),
        currently_printing=len(
            await live_prints_blocking(
                db,
                scope=(
                    (PrintQueueItem.created_by_id == user_id)
                    | PrintQueueItem.archive_id.in_(archive_ids)
                    | PrintQueueItem.library_file_id.in_(library_file_ids)
                    | PrintQueueItem.batch_id.in_(batch_ids)
                ),
            )
        ),
    )


async def _destroy_owned_items(db: AsyncSession, user_id: int) -> list[Path]:
    """The ``delete_items=true`` branch. Returns the paths to purge after commit.

    Refuses by raising the 409 — see :func:`delete_user`. The phase ordering here
    is the module docstring's; the comments below name which phase each block is.
    """
    # --- Phase 1: the user's own queue items, through the storage-boundary
    # transition. First, so that the rollback on refusal is clean.
    _deleted, still_printing = await delete_user_items_unless_printing(db, user_id=user_id)
    blocking = list(still_printing)

    # --- Phase 2: read the estate while it still exists. The rows carry the
    # on-disk paths, so resolution has to happen before the DELETEs, and the
    # ids are what every phase below is scoped by.
    archives = list((await db.execute(select(PrintArchive).where(PrintArchive.created_by_id == user_id))).scalars())
    library_files = list((await db.execute(select(LibraryFile).where(LibraryFile.created_by_id == user_id))).scalars())
    batch_ids = [
        row[0] for row in (await db.execute(select(PrintBatch.id).where(PrintBatch.created_by_id == user_id))).all()
    ]
    archive_ids = [a.id for a in archives]
    library_file_ids = [f.id for f in library_files]

    doomed: list[Path] = [d for a in archives if (d := resolve_archive_dir_for_delete(a)) is not None]
    doomed += [path for f in library_files for path in owned_paths(f)]

    # --- Phase 3: every dependent that does NOT live in print_queue. Runs before
    # the parent DELETEs because a NO-ACTION FK would abort them on Postgres, and
    # safe to run before the refusal is decided because a refusal rolls it back.
    sku_file_ids = [
        row[0]
        for row in (await db.execute(select(SkuFile.id).where(SkuFile.library_file_id.in_(library_file_ids)))).all()
    ]
    await null_print_log_thumbnail_paths(db, archive_ids)
    await _apply_declared_fk_policy(db, _ARCHIVE_DEPENDENTS, archive_ids)
    await _apply_declared_fk_policy(db, _SKU_FILE_DEPENDENTS, sku_file_ids)
    await _apply_declared_fk_policy(db, _LIBRARY_FILE_DEPENDENTS, library_file_ids)

    # --- Phase 4: the parent deletes, each guarded by its own correlated NOT
    # EXISTS. Set-based, one statement per table, and a row a live print is
    # running off is simply not matched — then read back, because what the
    # statement DECLINED to touch is the guard's own verdict.
    refused: dict[str, list[int]] = {}
    for label, model, scope, ref_col in (
        ("archives", PrintArchive, PrintArchive.created_by_id == user_id, PrintQueueItem.archive_id),
        ("library files", LibraryFile, LibraryFile.created_by_id == user_id, PrintQueueItem.library_file_id),
        ("batches", PrintBatch, PrintBatch.created_by_id == user_id, PrintQueueItem.batch_id),
    ):
        _deleted, survivors = await _delete_backing_rows_unless_printing(db, model, scope, ref_col)
        if survivors:
            refused[label] = survivors

    # --- Phase 5: read the blockers back, in this transaction, across every
    # scope. Phase 4 touched no queue rows, so this reads the same evidence the
    # WHERE clauses did.
    blocking += await live_prints_blocking(db, scope=PrintQueueItem.archive_id.in_(archive_ids))
    blocking += await live_prints_blocking(db, scope=PrintQueueItem.library_file_id.in_(library_file_ids))
    blocking += await live_prints_blocking(db, scope=PrintQueueItem.batch_id.in_(batch_ids))

    # --- Phase 6: EITHER signal refuses everything, because they catch different
    # races and neither subsumes the other.
    #
    # ``refused`` is what was live when the DELETE ran. ``blocking`` is what is
    # live now, over the ids phase 2 captured — a wider net that also catches a
    # unit which became ``printing`` AFTER its parent was already deleted.
    #
    # Only ``refused`` closes the narrow window between them: a unit printing at
    # phase 4 makes the guard skip its archive, and if that print FINISHES before
    # phase 5 reads, ``blocking`` is empty and the request would commit — leaving
    # that archive in place with ``created_by_id`` pointing at a user who no
    # longer exists. A dangling row of precisely the class this module was written
    # to stop creating, manufactured by the module itself.
    #
    # Printer names come from ``blocking`` alone, and that is not an oversight:
    # survivors are a subset of the ids phase 5 queried, so anything still
    # printing off one is already in ``blocking``. A survivor absent from it is
    # the finish-in-the-window case above, where the print is over and there is no
    # printer left to name. It still refuses — proceeding because the sentence
    # would be thin is the failure mode, not the thin sentence.
    if blocking or refused:
        blocking = sorted(set(blocking))
        logger.info(
            "user_deletion: refusing user %s — %d unit(s) printing at write time: %s; rows the guard declined: %s",
            user_id,
            len(blocking),
            blocking,
            refused or "none",
        )
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=await printing_units_conflict(db, blocking, code=_CONFLICT_CODE, subject=_CONFLICT_SUBJECT),
        )

    # --- Phase 7: the print_queue dependents, now that nothing in scope is
    # printing and the evidence has served its purpose.
    await delete_related_queue_items(
        db,
        PrintQueueItem.archive_id.in_(archive_ids) | PrintQueueItem.library_file_id.in_(library_file_ids),
    )
    await _apply_declared_fk_policy(db, _BATCH_DEPENDENTS, batch_ids)

    logger.info(
        "user_deletion: user %s — deleted %d archive(s), %d library file(s), %d batch(es); %d path(s) to purge",
        user_id,
        len(archive_ids),
        len(library_file_ids),
        len(batch_ids),
        len(doomed),
    )
    return doomed


async def _disown_owned_items(db: AsyncSession, user_id: int) -> None:
    """The ``delete_items=false`` branch: everything the user made becomes ownerless.

    Explicit UPDATEs rather than a reliance on ``ON DELETE SET NULL``, for the
    same reason phase 3 above is explicit: SQLite enforces no FK, so without these
    the rows would keep a ``created_by_id`` pointing at a user that no longer
    exists — and ``created_by_id`` is what the Archives / Print Log / File Manager
    views join a username from.
    """
    for column in (
        PrintArchive.created_by_id,
        PrintQueueItem.created_by_id,
        LibraryFile.created_by_id,
        PrintBatch.created_by_id,
    ):
        await db.execute(update(column.parent.class_).where(column == user_id).values({column: None}))


async def _delete_user_aggregate_rows(db: AsyncSession, user_id: int) -> None:
    """Rows that belong to the ACCOUNT rather than to anything it created.

    Runs on BOTH branches — ``delete_items`` decides the fate of the user's work,
    never of their credentials or of the attribution columns on other estates'
    rows. Each statement performs the policy its model declares; every one of them
    is a no-op on Postgres, where the engine has already run it.

    The credential rows are CASCADE and must actually go. Left dangling on SQLite:
    an ``APIKey`` whose user cannot be resolved degrades to anonymous rather than
    failing (``_user_from_api_key`` returns None, and only ``/cloud/*`` rejects
    that — the rest of the API accepts the key, which is precisely the orphan-key
    state the CASCADE exists to prevent); a ``UserOIDCLink`` makes the OIDC
    callback resolve a missing user and fall through to ``account_inactive``
    instead of triggering auto-create (#1285); ``UserTOTP`` / ``UserOTPCode`` leave
    MFA secrets and pending codes behind the account; and a ``LongLivedToken``
    keeps a camera-stream secret_hash that ``verify()`` still matches by prefix.

    Two of these were unhandled by BOTH branches until this module existed. Both
    are SET NULL and join the updates below: ``PrintLogEntry`` is the authoritative
    print history and is never deleted by a user delete, only de-attributed; and
    ``SlotRecheckIntent.requested_by`` is SET NULL by deliberate design, because
    deleting an operator must not erase a slot's identity history.
    """
    for model, column in (
        (APIKey, APIKey.user_id),
        (UserOIDCLink, UserOIDCLink.user_id),
        (UserTOTP, UserTOTP.user_id),
        (UserOTPCode, UserOTPCode.user_id),
        (LongLivedToken, LongLivedToken.user_id),
    ):
        await db.execute(delete(model).where(column == user_id))

    for column in (PrintLogEntry.created_by_id, SlotRecheckIntent.requested_by):
        await db.execute(update(column.parent.class_).where(column == user_id).values({column: None}))


async def _delete_backing_rows_unless_printing(
    db: AsyncSession,
    model: type[_BackingRow],
    scope: ColumnElement[bool],
    ref_col: InstrumentedAttribute[int | None],
) -> tuple[int, list[int]]:
    """``DELETE FROM <model> WHERE <scope> AND NOT EXISTS (printing item via ref_col)``.

    One statement per table, evaluated by the database — the parent-row mirror of
    ``queue_transitions._delete_unless_printing``, and correct under concurrency
    for the same reason: a unit that dispatches between this request's SELECT and
    its DELETE is seen by the WHERE clause, not by a stale Python list.

    Returns ``(deleted_count, survivor_ids)``, the survivors read back inside the
    SAME transaction — and the survivors are the load-bearing half. A row still
    present after its own DELETE is a row the statement DECLINED to touch, which
    is a refusal in its own right and the only direct evidence that the guard
    fired at all. Without reading it back the predicate is unobservable: the
    outcome is decided further down by a separate query, so deleting
    ``~printing_items_referencing(ref_col)`` from the WHERE changes nothing any
    caller or test can see (measured — that mutation survived the whole suite).

    ANY survivor refuses, exactly as in the queue-item sibling: a row that reached
    this scope by a route the module did not anticipate fails safe rather than
    being quietly left behind while the rest of the estate is destroyed around it.
    """
    result = await db.execute(
        delete(model)
        .where(scope, ~printing_items_referencing(ref_col))
        .returning(model.id)
        .execution_options(synchronize_session=False)
    )
    deleted = [row[0] for row in result.all()]

    survived = await db.execute(select(model.id).where(scope))
    return len(deleted), sorted(row[0] for row in survived.all())


async def _apply_declared_fk_policy(
    db: AsyncSession,
    dependents: Sequence[_DeclaredFk],
    parent_ids: Sequence[int],
) -> None:
    """Run each declared ``ondelete`` in *dependents* against *parent_ids*.

    The one helper every dependent in this module goes through, so "what happens
    to the children" is answered by the declaration table above rather than by a
    reader tracing statements. Empty *parent_ids* is a no-op — an ``IN ()`` that
    matched nothing would be harmless, but the round trip is not free and a user
    with no archives is the common case.
    """
    ids = list(parent_ids)
    if not ids:
        return
    for dependent in dependents:
        model = dependent.column.parent.class_
        if dependent.policy == "CASCADE":
            await db.execute(delete(model).where(dependent.column.in_(ids)))
        else:
            await db.execute(update(model).where(dependent.column.in_(ids)).values({dependent.column: None}))


def _owned_ids(model: type[_BackingRow], user_id: int) -> ScalarSelect[int]:
    """Scalar subquery of the ids *user_id* created in *model*.

    A subquery rather than a materialised list because :func:`delete_impact` only
    ever feeds it to ``IN``, and a count endpoint has no reason to pull an
    unbounded id list into Python to hand it straight back to the database.
    """
    return select(model.id).where(model.created_by_id == user_id).scalar_subquery()


async def _count(
    db: AsyncSession,
    model: type[PrintArchive] | type[LibraryFile] | type[PrintBatch] | type[PrintQueueItem],
    scope: ColumnElement[bool],
) -> int:
    rows = await db.execute(select(func.count()).select_from(model).where(scope))
    return int(rows.scalar_one() or 0)


async def _count_distinct(db: AsyncSession, column: InstrumentedAttribute[int], scope: ColumnElement[bool]) -> int:
    rows = await db.execute(select(func.count(func.distinct(column))).where(scope))
    return int(rows.scalar_one() or 0)
