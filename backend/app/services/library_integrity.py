"""Library file integrity sweep — detection for the silent-byte-loss class.

On 2026-08-14 a farm bug deleted a library file's bytes while its DB row stayed
active; nothing noticed until dispatches started failing against a row that
pointed at nothing. That specific deleter is fixed, but an audit cannot prove no
OTHER deleter exists — so the closure ships with a liveness probe, per house
doctrine: a class of silent failure needs a detector, not just a patched cause.

The sweep answers one question per active row — "are the bytes still there?" —
and is deliberately READ-ONLY. It never repairs, never soft-deletes and never
touches the row: an absent file may equally mean a detached network share or a
half-finished restore, and a sweeper that "cleaned up" those would become the
very deleter it exists to catch.

Rows skipped, and why:
  * ``deleted_at`` non-null — trashed rows are expected to lose their bytes.
  * ``is_external`` — external bytes live outside our management (user-mounted
    folders), so their absence is the user's business, not a farm fault.

Modeled on the ``log_maintenance`` / ``archive_purge`` sweeper shape
(start/stop scheduler + asyncio.sleep loop with CancelledError-aware teardown),
differing in one respect: this loop sweeps BEFORE its first sleep, because the
interesting moment for an integrity check is a restart — that is when an
operator is most likely already looking, and when a still-missing file should
re-announce itself.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core import database as _database
from backend.app.models.library import LibraryFile

logger = logging.getLogger(__name__)

# Daily cadence — this is a backstop detector, not a hot path. The dispatch
# failure the missing bytes cause is itself immediate; this exists to name the
# cause before someone has to reverse-engineer it from a 404.
_CHECK_INTERVAL_SECONDS = 24 * 60 * 60


class LibraryIntegrityService:
    """Daily sweep announcing active library rows whose bytes are gone."""

    def __init__(self) -> None:
        self._scheduler_task: asyncio.Task | None = None
        self._check_interval = _CHECK_INTERVAL_SECONDS
        # Announce-once ledger, per process lifetime. A restart deliberately
        # re-announces a still-missing file: the log line is the record, and a
        # fresh boot is exactly when it is worth restating.
        self._announced: set[int] = set()

    async def start_scheduler(self) -> None:
        if self._scheduler_task is not None:
            return
        logger.info("Starting library file integrity sweep")
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    def stop_scheduler(self) -> None:
        if self._scheduler_task:
            self._scheduler_task.cancel()
            self._scheduler_task = None
            logger.info("Stopped library file integrity sweep")

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                async with _database.async_session() as db:
                    await self.sweep(db)
                await asyncio.sleep(self._check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:  # pragma: no cover - defensive
                logger.error("Error in library file integrity sweep: %s", e)
                await asyncio.sleep(60)

    async def sweep(self, db: AsyncSession) -> list[int]:
        """Warn about every active, managed library row whose bytes are missing.

        Returns the ids of ALL rows found missing this pass — the return value is
        the sweep's honest verdict, while the WARNING is deduped by
        :attr:`_announced`, so a caller (or a test) can tell "still missing" from
        "newly announced".
        """
        # Lazy import: ``to_absolute_path`` is THE path resolver the library
        # routes use, and resolving any other way would let this detector
        # disagree with the code whose files it is checking. It lives in the
        # route module, so importing it at module scope would both invert the
        # route->service layering at import time and risk a cycle (that module
        # imports services); the call-site import is the same accommodation
        # ``api/routes/archives.py`` makes for its library helpers.
        from backend.app.api.routes.library import to_absolute_path

        result = await db.execute(LibraryFile.active().where(LibraryFile.is_external.is_(False)))
        missing: list[int] = []
        for row in result.scalars().all():
            resolved, reason = self._resolve(to_absolute_path, row.file_path)
            if reason is None:
                continue
            missing.append(row.id)
            if row.id in self._announced:
                continue
            self._announced.add(row.id)
            logger.warning(
                "library integrity: file id=%s %r has no bytes on disk (%s) — expected at %s; "
                "the row is still active and will fail any dispatch that reaches for it",
                row.id,
                row.filename,
                reason,
                resolved if resolved is not None else row.file_path,
            )
        return missing

    @staticmethod
    def _resolve(
        to_absolute_path: Callable[[str | None], Path | None], file_path: str | None
    ) -> tuple[Path | None, str | None]:
        """``(resolved_path, reason_missing)``; ``reason_missing`` None = bytes present.

        An unresolvable path counts as missing: a row nobody can turn into a
        filename is exactly as undispatchable as one whose file was deleted.
        """
        if not file_path:
            return None, "row has no file_path"
        try:
            resolved = to_absolute_path(file_path)
        except ValueError as e:
            # to_absolute_path refuses a stored path that escapes base_dir.
            return None, f"unresolvable path ({e})"
        if resolved is None:
            return None, "row has no file_path"
        try:
            if resolved.is_file():
                return resolved, None
        except OSError as e:
            # An unreadable mount answers neither yes nor no — report it as
            # missing rather than silently passing a row we could not verify.
            return resolved, f"path not readable ({e})"
        return resolved, "file does not exist"


library_integrity_service = LibraryIntegrityService()
