"""Guard tests for the directory an archive delete is allowed to rmtree.

``archive.resolve_archive_dir_for_delete`` resolves a DB column
(``PrintArchive.file_path``) into a directory that is then handed to
``shutil.rmtree`` — including from the unattended archive-purge sweeper. The
old rule only demanded "at least one level under archive_dir", which the
managed library (``archive/library/files``, two levels) and the shared 3MF
scratch dir (``archive/temp``) both satisfy: one archive row pointing there
would have wiped every user upload in the install.

The rule under test: a deletable directory is EXACTLY ``<printer>/<archive>``
under ``settings.archive_dir`` — the shape ``ArchiveService._ingest_3mf``
creates — and its first part is never a reserved sibling estate.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.services import archive as archive_module
from backend.app.services.archive import ArchiveService, resolve_archive_dir_for_delete


@pytest.fixture
def archive_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the service at a temp data dir laid out like production."""
    monkeypatch.setattr(archive_module.settings, "base_dir", tmp_path)
    monkeypatch.setattr(archive_module.settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _seed_file(base_dir: Path, relative_path: str) -> Path:
    """Create ``base_dir/relative_path`` (and parents) with dummy bytes."""
    target = base_dir / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"PK\x03\x04")
    return target


async def _add_archive(db_session, relative_path: str) -> PrintArchive:
    archive = PrintArchive(
        printer_id=None,
        filename=Path(relative_path).name,
        print_name="Guard Test",
        file_path=relative_path,
        file_size=4,
        status="completed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)
    return archive


class TestResolveArchiveDirForDelete:
    """The accept/refuse table for the rmtree target."""

    async def test_accepts_legitimate_archive_layout(self, db_session, archive_root: Path):
        """``archive/<printer>/<archive folder>/file.3mf`` — what _ingest_3mf writes."""
        rel = "archive/7/20260814_120000_part/part.gcode.3mf"
        _seed_file(archive_root, rel)
        archive = await _add_archive(db_session, rel)

        resolved = resolve_archive_dir_for_delete(archive)

        assert resolved == (archive_root / "archive/7/20260814_120000_part")

    async def test_accepts_unassigned_printer_folder(self, db_session, archive_root: Path):
        """Archives without a printer land under ``unassigned`` — same depth."""
        rel = "archive/unassigned/20260814_120000_part/part.gcode.3mf"
        _seed_file(archive_root, rel)
        archive = await _add_archive(db_session, rel)

        resolved = resolve_archive_dir_for_delete(archive)

        assert resolved == (archive_root / "archive/unassigned/20260814_120000_part")

    @pytest.mark.parametrize(
        "rel",
        [
            "archive/library/files/user_upload.3mf",  # the managed library (L1)
            "archive/temp/session/scratch.3mf",  # shared 3MF scratch space (L2)
            "archive/7/only_one_level.3mf",  # depth 1: a whole printer's archives
            "archive/directly_in_root.3mf",  # depth 0: the archive root itself
            "archive/7/20260814_120000_part/nested/deep.3mf",  # depth 3
        ],
    )
    async def test_refuses_non_archive_directories(
        self,
        db_session,
        archive_root: Path,
        caplog: pytest.LogCaptureFixture,
        rel: str,
    ):
        _seed_file(archive_root, rel)
        archive = await _add_archive(db_session, rel)

        with caplog.at_level("WARNING", logger="backend.app.services.archive"):
            resolved = resolve_archive_dir_for_delete(archive)

        assert resolved is None
        # Never a silent skip: the refusal names the archive and the path.
        refusals = [r for r in caplog.records if "Refusing to delete" in r.getMessage()]
        assert refusals, "refusal must be logged"
        message = refusals[-1].getMessage()
        assert str(archive.id) in message
        assert str((archive_root / rel).parent) in message

    async def test_refuses_path_outside_archive_dir(self, db_session, archive_root: Path):
        rel = "elsewhere/private/secret.3mf"
        _seed_file(archive_root, rel)
        archive = await _add_archive(db_session, rel)

        assert resolve_archive_dir_for_delete(archive) is None

    async def test_refuses_empty_file_path(self, db_session, archive_root: Path):
        archive = await _add_archive(db_session, "")

        assert resolve_archive_dir_for_delete(archive) is None


class TestDeleteArchiveHonoursTheGuard:
    """``delete_archive`` shares the helper — it no longer carries its own rule."""

    async def test_hard_delete_removes_only_the_archive_directory(self, db_session, archive_root: Path):
        rel = "archive/7/20260814_120000_part/part.gcode.3mf"
        _seed_file(archive_root, rel)
        sibling = _seed_file(archive_root, "archive/7/20260814_130000_other/other.gcode.3mf")
        archive = await _add_archive(db_session, rel)

        assert await ArchiveService(db_session).delete_archive(archive.id) is True

        assert not (archive_root / rel).parent.exists()
        assert sibling.exists()
        assert await db_session.get(PrintArchive, archive.id) is None

    async def test_hard_delete_refuses_to_rmtree_the_library(self, db_session, archive_root: Path):
        """The data-loss shape: an archive row pointing at the managed library."""
        rel = "archive/library/files/user_upload.3mf"
        upload = _seed_file(archive_root, rel)
        neighbour = _seed_file(archive_root, "archive/library/files/other_upload.3mf")
        archive = await _add_archive(db_session, rel)

        assert await ArchiveService(db_session).delete_archive(archive.id) is True

        # Files untouched...
        assert upload.exists()
        assert neighbour.exists()
        assert upload.parent.exists()
        # ...while the DB-side behaviour is unchanged: the row still goes.
        assert await db_session.get(PrintArchive, archive.id) is None

    async def test_hard_delete_refuses_to_rmtree_the_temp_dir(self, db_session, archive_root: Path):
        rel = "archive/temp/session/scratch.3mf"
        scratch = _seed_file(archive_root, rel)
        archive = await _add_archive(db_session, rel)

        assert await ArchiveService(db_session).delete_archive(archive.id) is True

        assert scratch.exists()
        assert await db_session.get(PrintArchive, archive.id) is None

    async def test_soft_delete_refuses_to_rmtree_the_library(self, db_session, archive_root: Path):
        """The unattended purge sweeper's default lane takes the same rule."""
        rel = "archive/library/files/user_upload.3mf"
        upload = _seed_file(archive_root, rel)
        archive = await _add_archive(db_session, rel)

        assert await ArchiveService(db_session).soft_delete_archive(archive.id) is True

        assert upload.exists()
        row = await db_session.get(PrintArchive, archive.id)
        assert row is not None
        assert row.deleted_at is not None

    async def test_soft_delete_removes_a_legitimate_archive_directory(self, db_session, archive_root: Path):
        rel = "archive/7/20260814_120000_part/part.gcode.3mf"
        _seed_file(archive_root, rel)
        archive = await _add_archive(db_session, rel)

        assert await ArchiveService(db_session).soft_delete_archive(archive.id) is True

        assert not (archive_root / rel).parent.exists()
        row = await db_session.get(PrintArchive, archive.id)
        assert row is not None and row.deleted_at is not None
