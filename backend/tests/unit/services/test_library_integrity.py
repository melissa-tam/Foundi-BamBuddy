"""Tests for the library file integrity sweep (services.library_integrity).

The deleter that removed a library file's bytes while its row stayed active is
fixed, but no audit can prove there is no other one — so this detector is the
liveness half of that closure, and these tests pin the two properties that make
it worth having: it names the missing row, and it stays read-only.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.core.config import settings as app_settings
from backend.app.models.library import LibraryFile
from backend.app.services import library_integrity as li_mod
from backend.app.services.library_integrity import LibraryIntegrityService

pytestmark = pytest.mark.asyncio

_LOGGER_NAME = "backend.app.services.library_integrity"


@pytest.fixture
def base_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``to_absolute_path`` resolves stored relative paths against this."""
    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    return tmp_path


@pytest.fixture
def service() -> LibraryIntegrityService:
    """A fresh instance — the singleton's announce-once ledger is process-lived,
    so sharing it across tests would let one test's warning silence another's."""
    return LibraryIntegrityService()


async def _add_file(
    db_session,
    base_dir: Path,
    relative_path: str,
    *,
    write_bytes: bool = True,
    is_external: bool = False,
    deleted_at: datetime | None = None,
) -> LibraryFile:
    """Create a library row, optionally with its bytes on disk under ``base_dir``."""
    if write_bytes:
        on_disk = base_dir / relative_path
        on_disk.parent.mkdir(parents=True, exist_ok=True)
        on_disk.write_bytes(b"PK\x03\x04")

    row = LibraryFile(
        filename=Path(relative_path).name,
        file_path=relative_path,
        file_type="3mf",
        file_size=4,
        is_external=is_external,
        deleted_at=deleted_at,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _LOGGER_NAME and r.levelno == logging.WARNING]


class TestIntegritySweepScope:
    """Exactly one class of row is a farm fault: active, managed, and byte-less."""

    async def test_only_the_missing_managed_row_is_reported(self, db_session, base_dir, service, caplog):
        present = await _add_file(db_session, base_dir, "archive/library/files/here.3mf")
        missing = await _add_file(db_session, base_dir, "archive/library/files/gone.3mf", write_bytes=False)
        # External bytes live outside our management — their absence is not our fault.
        await _add_file(db_session, base_dir, str(base_dir / "elsewhere" / "ext.3mf"), write_bytes=False, is_external=True)
        # A trashed row is EXPECTED to lose its bytes; announcing it would be noise.
        await _add_file(
            db_session,
            base_dir,
            "archive/library/files/trashed.3mf",
            write_bytes=False,
            deleted_at=datetime.now(timezone.utc) - timedelta(days=1),
        )

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            reported = await service.sweep(db_session)

        assert reported == [missing.id]
        assert present.id not in reported
        messages = _warnings(caplog)
        assert len(messages) == 1
        # The line has to be actionable on its own: which row, which file, where.
        assert f"id={missing.id}" in messages[0]
        assert "gone.3mf" in messages[0]
        assert str(base_dir / "archive" / "library" / "files" / "gone.3mf") in messages[0]

    async def test_an_unresolvable_path_counts_as_missing(self, db_session, base_dir, service, caplog):
        # to_absolute_path refuses a stored path that escapes base_dir. A row nobody
        # can turn into a filename is exactly as undispatchable as a deleted one.
        row = await _add_file(db_session, base_dir, "../outside-the-base.3mf", write_bytes=False)

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            reported = await service.sweep(db_session)

        assert reported == [row.id]
        assert "unresolvable path" in _warnings(caplog)[0]

    async def test_an_all_healthy_library_is_silent(self, db_session, base_dir, service, caplog):
        await _add_file(db_session, base_dir, "archive/library/files/a.3mf")
        await _add_file(db_session, base_dir, "archive/library/files/b.3mf")

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            assert await service.sweep(db_session) == []

        assert _warnings(caplog) == []

    async def test_the_sweep_never_mutates_the_row(self, db_session, base_dir, service):
        # Read-only by design: an absent file may be a detached share or a half-finished
        # restore, and a sweeper that "tidied up" would become the deleter it detects.
        row = await _add_file(db_session, base_dir, "archive/library/files/gone.3mf", write_bytes=False)

        await service.sweep(db_session)

        await db_session.refresh(row)
        assert row.deleted_at is None
        assert row.file_path == "archive/library/files/gone.3mf"


class TestIntegritySweepDedup:
    """Announce once per file per process — the log is a record, not a metronome."""

    async def test_a_still_missing_file_is_announced_once_across_two_sweeps(
        self, db_session, base_dir, service, caplog
    ):
        row = await _add_file(db_session, base_dir, "archive/library/files/gone.3mf", write_bytes=False)

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            first = await service.sweep(db_session)
            second = await service.sweep(db_session)

        # The verdict is honest every pass; only the WARNING is deduped.
        assert first == [row.id] and second == [row.id]
        assert len(_warnings(caplog)) == 1

    async def test_a_fresh_process_re_announces(self, db_session, base_dir, service, caplog):
        # The ledger is per-process on purpose: a restart is exactly when a still-missing
        # file is worth restating, because that is when someone is looking.
        await _add_file(db_session, base_dir, "archive/library/files/gone.3mf", write_bytes=False)

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            await service.sweep(db_session)
            await LibraryIntegrityService().sweep(db_session)

        assert len(_warnings(caplog)) == 2


class TestIntegritySweepScheduler:
    """Scheduler lifecycle, mirroring the log_maintenance / archive_purge shape."""

    async def test_start_is_idempotent_and_stop_cancels(self, service):
        with patch("asyncio.create_task", MagicMock(return_value=MagicMock())) as create_task:
            await service.start_scheduler()
            await service.start_scheduler()
        create_task.assert_called_once()
        task = service._scheduler_task
        service.stop_scheduler()
        task.cancel.assert_called_once()
        assert service._scheduler_task is None

    async def test_stop_without_start_is_a_noop(self, service):
        service.stop_scheduler()  # must not raise
        assert service._scheduler_task is None

    async def test_the_loop_sweeps_before_its_first_sleep(self, service):
        """Startup is the interesting moment — a sleep-first loop would hide a
        still-missing file for 24 h after the restart that should re-announce it."""
        calls: list[str] = []

        async def _sweep(db):
            calls.append("sweep")
            return []

        async def _sleep(delay):
            calls.append(f"sleep:{delay}")
            raise asyncio.CancelledError

        session = MagicMock()
        session.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        session.return_value.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(li_mod._database, "async_session", session),
            patch.object(service, "sweep", _sweep),
            patch("asyncio.sleep", _sleep),
        ):
            await service._scheduler_loop()

        assert calls == ["sweep", f"sleep:{service._check_interval}"]
        assert service._check_interval == 24 * 60 * 60


class TestLifespanRegistration:
    """REGISTRATION PIN: a detector nobody starts detects nothing.

    Pinned at the source level rather than by driving the lifespan — importing and
    running ``main``'s startup would stand up the whole server (MQTT sessions,
    schedulers, DB migrations) to assert two lines. Same precedent as
    ``test_dispatch.py::test_scheduler_has_no_eject_injection_branch``.
    """

    _MAIN = Path(__file__).resolve().parents[4] / "backend" / "app" / "main.py"

    async def test_the_sweep_is_started_and_stopped_by_the_lifespan(self):
        src = self._MAIN.read_text(encoding="utf-8")
        assert "from backend.app.services.library_integrity import library_integrity_service" in src
        assert "await library_integrity_service.start_scheduler()" in src
        assert "library_integrity_service.stop_scheduler()" in src
