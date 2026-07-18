"""NSSM service-log maintenance sweeper.

The Windows service wrapper (NSSM) redirects the server's stdout/stderr to
``service-stdout.log`` / ``service-stderr.log`` in ``LOG_DIR`` and rotates them
online by size (``AppRotateBytes``), but with NO count or age cap — the
timestamped rotated siblings (e.g. ``service-stdout-2026-07-17_00-00-00.log``)
accumulate forever. This sweeper age-purges those rotated siblings, mirroring
the ``backupCount`` cap the ``TimedRotatingFileHandler`` already applies to
``bambuddy.log``.

It deliberately NEVER touches ``bambuddy.log*`` — that rotation class is owned by
the logging handler's ``backupCount`` — nor the two live NSSM names
(``service-stdout.log`` / ``service-stderr.log``), which NSSM writes to directly.

Modeled on the ``archive_purge`` sweeper loop shape (start/stop scheduler +
asyncio.sleep loop with CancelledError-aware teardown).
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

# The two ACTIVE NSSM names — never deleted (NSSM has them open for writing).
_NSSM_ACTIVE_NAMES = frozenset({"service-stdout.log", "service-stderr.log"})
# Glob families that capture the active names AND their timestamped rotations.
# The active names are re-excluded by _NSSM_ACTIVE_NAMES so only rotations age
# out. Neither pattern can match ``bambuddy.log*``.
_NSSM_GLOBS = ("service-stdout*.log", "service-stderr*.log")

# Daily cadence — this is pure filesystem housekeeping, no need to run hot.
_CHECK_INTERVAL_SECONDS = 24 * 60 * 60


class LogMaintenanceService:
    """Daily age-purge of NSSM's timestamped rotated service logs."""

    def __init__(self) -> None:
        self._scheduler_task: asyncio.Task | None = None
        self._check_interval = _CHECK_INTERVAL_SECONDS

    async def start_scheduler(self) -> None:
        if self._scheduler_task is not None:
            return
        logger.info("Starting NSSM service-log maintenance sweeper")
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    def stop_scheduler(self) -> None:
        if self._scheduler_task:
            self._scheduler_task.cancel()
            self._scheduler_task = None
            logger.info("Stopped NSSM service-log maintenance sweeper")

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._check_interval)
                self.purge_rotated_service_logs()
            except asyncio.CancelledError:
                break
            except Exception as e:  # pragma: no cover - defensive
                logger.error("Error in NSSM service-log maintenance sweeper: %s", e)
                await asyncio.sleep(60)

    def purge_rotated_service_logs(self, *, retention_days: int | None = None) -> int:
        """Delete NSSM rotated service logs older than the retention window.

        Only timestamped rotated siblings of ``service-stdout.log`` /
        ``service-stderr.log`` are eligible: the two active names are always
        preserved, and ``bambuddy.log*`` is never matched (that rotation is owned
        by the ``TimedRotatingFileHandler``'s ``backupCount``). ``retention_days``
        defaults to :data:`settings.log_retention_days`. Returns the number of
        files deleted.
        """
        days = settings.log_retention_days if retention_days is None else retention_days
        cutoff = time.time() - days * 86400
        log_dir = Path(settings.log_dir)

        deleted = 0
        for pattern in _NSSM_GLOBS:
            for path in log_dir.glob(pattern):
                if path.name in _NSSM_ACTIVE_NAMES:
                    continue
                try:
                    if not path.is_file():
                        continue
                    if path.stat().st_mtime >= cutoff:
                        continue
                    path.unlink()
                    deleted += 1
                except OSError as e:
                    logger.error("Failed to delete rotated service log %s: %s", path, e)
        if deleted:
            logger.info(
                "NSSM log maintenance: deleted %d rotated service log(s) older than %d days",
                deleted,
                days,
            )
        return deleted


log_maintenance_service = LogMaintenanceService()
