"""Folder delete routes its members through the trash lane (#1008 follow-up).

``DELETE /library/folders/{id}`` used to be the only managed-library delete
that bypassed the trash: it read ``LibraryFile.file_path`` RAW from the DB and
called ``os.remove`` on it, so a relative row resolved against the process CWD
— normally deleting nothing (bytes orphaned forever, row gone via the folder
cascade), and hard-deleting the real file whenever the CWD happened to be
``base_dir``. Every member now takes the same soft-delete lane as a single-file
delete: restorable until the retention sweeper collects it.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.api.routes.library import bulk_delete, delete_folder
from backend.app.core.config import settings as app_settings
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.schemas.library import BulkDeleteRequest
from backend.app.services.library_trash import library_trash_service


@pytest.fixture
def base_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    return tmp_path


async def _add_folder(db_session, name: str, parent_id: int | None = None, **kwargs) -> LibraryFolder:
    folder = LibraryFolder(name=name, parent_id=parent_id, **kwargs)
    db_session.add(folder)
    await db_session.commit()
    await db_session.refresh(folder)
    return folder


async def _add_file(
    db_session,
    base_dir: Path,
    folder_id: int | None,
    relative_path: str,
    *,
    is_external: bool = False,
    thumbnail_relative_path: str | None = None,
    deleted_at: datetime | None = None,
) -> LibraryFile:
    """Create a library row AND its bytes under ``base_dir``."""
    on_disk = base_dir / relative_path
    on_disk.parent.mkdir(parents=True, exist_ok=True)
    on_disk.write_bytes(b"PK\x03\x04")
    if thumbnail_relative_path:
        thumb = base_dir / thumbnail_relative_path
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"\x89PNG")

    file = LibraryFile(
        folder_id=folder_id,
        filename=Path(relative_path).name,
        file_path=relative_path,
        file_type="3mf",
        file_size=4,
        is_external=is_external,
        thumbnail_path=thumbnail_relative_path,
        deleted_at=deleted_at,
    )
    db_session.add(file)
    await db_session.commit()
    await db_session.refresh(file)
    return file


class TestFolderDeleteUsesTheTrashLane:
    async def test_managed_members_are_soft_deleted_and_bytes_stay(self, db_session, base_dir: Path):
        folder = await _add_folder(db_session, "Parts")
        child = await _add_folder(db_session, "Nested", parent_id=folder.id)
        top = await _add_file(db_session, base_dir, folder.id, "archive/library/files/top.3mf")
        nested = await _add_file(db_session, base_dir, child.id, "archive/library/files/nested.3mf")

        await delete_folder(folder.id, db_session, None)

        for file in (top, nested):
            row = await db_session.get(LibraryFile, file.id)
            assert row is not None, "a managed upload must survive a folder delete as a trashed row"
            assert row.deleted_at is not None
            # Detached so the folder cascade cannot take it; restores to root.
            assert row.folder_id is None
            assert (base_dir / row.file_path).exists(), "bytes belong to the sweeper, not this route"

        # Folder rows themselves are gone (that is what the user asked for).
        assert await db_session.get(LibraryFolder, folder.id) is None
        assert await db_session.get(LibraryFolder, child.id) is None

    async def test_trashed_member_is_restorable(self, db_session, base_dir: Path):
        folder = await _add_folder(db_session, "Parts")
        file = await _add_file(db_session, base_dir, folder.id, "archive/library/files/top.3mf")

        await delete_folder(folder.id, db_session, None)

        trashed = await db_session.get(LibraryFile, file.id)
        assert trashed is not None
        await library_trash_service.restore(db_session, trashed)

        restored = (await db_session.execute(LibraryFile.active().where(LibraryFile.id == file.id))).scalar_one()
        assert restored.deleted_at is None
        assert restored.folder_id is None
        assert (base_dir / restored.file_path).exists()

    async def test_retention_sweep_then_removes_the_bytes(self, db_session, base_dir: Path):
        folder = await _add_folder(db_session, "Parts")
        file = await _add_file(
            db_session,
            base_dir,
            folder.id,
            "archive/library/files/top.3mf",
            thumbnail_relative_path="archive/library/thumbnails/top.png",
        )
        on_disk = base_dir / file.file_path
        thumb_on_disk = base_dir / "archive/library/thumbnails/top.png"

        await delete_folder(folder.id, db_session, None)
        assert on_disk.exists()

        # Age the trashed row past the retention window and run the sweeper.
        trashed = await db_session.get(LibraryFile, file.id)
        trashed.deleted_at = datetime.now(timezone.utc) - timedelta(days=90)
        await db_session.commit()

        swept = await library_trash_service._sweep(db_session)

        assert swept == 1
        assert await db_session.get(LibraryFile, file.id) is None
        assert not on_disk.exists()
        assert not thumb_on_disk.exists()

    async def test_already_trashed_member_keeps_its_stamp_and_survives(self, db_session, base_dir: Path):
        """The folder cascade must not hard-delete rows already in the trash."""
        folder = await _add_folder(db_session, "Parts")
        stamped_at = datetime(2026, 8, 1, 12, 0, 0)
        file = await _add_file(
            db_session,
            base_dir,
            folder.id,
            "archive/library/files/old.3mf",
            deleted_at=stamped_at,
        )

        await delete_folder(folder.id, db_session, None)

        row = await db_session.get(LibraryFile, file.id)
        assert row is not None
        assert row.deleted_at == stamped_at, "re-trashing must not restart the retention clock"
        assert (base_dir / row.file_path).exists()

    async def test_external_members_keep_their_bytes(self, db_session, base_dir: Path):
        """External semantics unchanged: only the thumbnail WE generated goes."""
        folder = await _add_folder(db_session, "Mount", is_external=True, external_path="/mnt/models")
        file = await _add_file(
            db_session,
            base_dir,
            folder.id,
            "external/models/part.3mf",
            is_external=True,
            thumbnail_relative_path="archive/library/thumbnails/part.png",
        )
        external_bytes = base_dir / file.file_path
        thumb_on_disk = base_dir / "archive/library/thumbnails/part.png"

        await delete_folder(folder.id, db_session, None)

        assert external_bytes.exists(), "external bytes are outside our control"
        assert not thumb_on_disk.exists()
        assert await db_session.get(LibraryFile, file.id) is None

    async def test_bulk_delete_folder_branch_takes_the_same_lane(self, db_session, base_dir: Path):
        folder = await _add_folder(db_session, "Parts")
        child = await _add_folder(db_session, "Nested", parent_id=folder.id)
        nested = await _add_file(db_session, base_dir, child.id, "archive/library/files/nested.3mf")

        response = await bulk_delete(
            BulkDeleteRequest(file_ids=[], folder_ids=[folder.id]),
            db_session,
            (None, True),
        )

        assert response.deleted_folders == 1
        assert response.deleted_files == 1
        row = await db_session.get(LibraryFile, nested.id)
        assert row is not None and row.deleted_at is not None
        assert (base_dir / row.file_path).exists()


class TestPathResolutionIsCwdIndependent:
    async def test_relative_rows_resolve_against_base_dir_not_cwd(
        self,
        db_session,
        base_dir: Path,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A decoy at the same relative path under the CWD must never be touched.

        The old route called ``os.remove(file_path)`` on the raw DB value, so
        whatever sat at that relative path under the process CWD was the file
        it actually deleted.
        """
        relative_path = "archive/library/files/top.3mf"
        folder = await _add_folder(db_session, "Parts")
        file = await _add_file(db_session, base_dir, folder.id, relative_path)

        elsewhere = tmp_path_factory.mktemp("cwd")
        decoy = elsewhere / relative_path
        decoy.parent.mkdir(parents=True, exist_ok=True)
        decoy.write_bytes(b"decoy")
        monkeypatch.chdir(elsewhere)

        await delete_folder(folder.id, db_session, None)

        assert decoy.read_bytes() == b"decoy"
        assert (base_dir / relative_path).exists()

        # The sweeper resolves the same way — it takes the real file, not the decoy.
        trashed = await db_session.get(LibraryFile, file.id)
        trashed.deleted_at = datetime.now(timezone.utc) - timedelta(days=90)
        await db_session.commit()
        await library_trash_service._sweep(db_session)

        assert decoy.read_bytes() == b"decoy"
        assert not (base_dir / relative_path).exists()

    async def test_no_managed_row_is_left_orphaned_by_the_folder_cascade(self, db_session, base_dir: Path):
        """Every member of the subtree is accounted for after the delete."""
        folder = await _add_folder(db_session, "Parts")
        child = await _add_folder(db_session, "Nested", parent_id=folder.id)
        grandchild = await _add_folder(db_session, "Deeper", parent_id=child.id)
        files = [
            await _add_file(db_session, base_dir, folder.id, "archive/library/files/a.3mf"),
            await _add_file(db_session, base_dir, child.id, "archive/library/files/b.3mf"),
            await _add_file(db_session, base_dir, grandchild.id, "archive/library/files/c.3mf"),
        ]
        outside = await _add_file(db_session, base_dir, None, "archive/library/files/root.3mf")

        await delete_folder(folder.id, db_session, None)

        surviving = (await db_session.execute(select(LibraryFile.id))).scalars().all()
        assert set(surviving) == {f.id for f in files} | {outside.id}
        untouched = await db_session.get(LibraryFile, outside.id)
        assert untouched.deleted_at is None
