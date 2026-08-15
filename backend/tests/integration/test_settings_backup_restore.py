"""Integration tests for backup honesty and transactional restore.

D5: ``create_backup_zip`` produced a ZIP even when data directories could not
be copied, and ``/settings/restore`` wiped each destination before copying —
so restoring a silently-partial backup destroyed ``archive/library/files``
(every user upload) with no way back.
"""

import contextlib
import io
import json
import shutil
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from backend.app.api.routes.settings import BACKUP_MANIFEST_NAME


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    """Point every backup/restore path at a throwaway data dir."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "plate_calibration_dir", tmp_path / "plate_calibration")
    return tmp_path


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_backup_zip(*, manifest: dict | None, dirs: dict[str, dict[str, str]] | None = None) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("bambuddy.db", b"SQLite format 3")
        if manifest is not None:
            zf.writestr(BACKUP_MANIFEST_NAME, json.dumps(manifest))
        for dir_name, files in (dirs or {}).items():
            for rel, content in files.items():
                zf.writestr(f"{dir_name}/{rel}", content)
    buf.seek(0)
    return buf


def _partial_manifest(dirs: list[str]) -> dict:
    return {
        "manifest_version": 1,
        "created_at": "2026-08-15T00:00:00+00:00",
        "app_version": "0.2.4.8",
        "build": "test",
        "included_dirs": dirs,
        "partial": True,
        "failures": [{"dir": d, "path": f"/data/{d}/locked.bin", "error": "Permission denied"} for d in dirs],
    }


def _clean_manifest(dirs: list[str]) -> dict:
    return {
        "manifest_version": 1,
        "created_at": "2026-08-15T00:00:00+00:00",
        "app_version": "0.2.4.8",
        "build": "test",
        "included_dirs": dirs,
        "partial": False,
        "failures": [],
    }


@contextlib.contextmanager
def _mocked_db_restore(import_pg=None):
    """Drive the restore endpoint without touching a real database."""
    with (
        patch("backend.app.core.db_dialect.is_sqlite", return_value=False),
        patch(
            "backend.app.api.routes.settings._import_sqlite_to_postgres",
            new=import_pg if import_pg is not None else AsyncMock(),
        ),
        patch("backend.app.core.database.close_all_connections", new_callable=AsyncMock),
        patch("backend.app.core.database.reinitialize_database", new_callable=AsyncMock),
        patch("backend.app.core.database.init_db", new_callable=AsyncMock),
    ):
        yield


def _aside_dirs(root: Path) -> list[Path]:
    return sorted(root.glob("*.pre-restore-*"))


class TestBackupManifest:
    """A backup ZIP must state whether it is complete."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_clean_backup_manifest(self, async_client: AsyncClient, data_dir):
        from backend.app.api.routes.settings import _read_manifest_from_zip, create_backup_zip

        _write(data_dir / "archive" / "library" / "files" / "part.3mf", "payload")

        zip_path, _filename = await create_backup_zip(output_path=data_dir)
        try:
            manifest = _read_manifest_from_zip(zip_path)
            assert manifest is not None
            assert manifest["partial"] is False
            assert manifest["failures"] == []
            assert "archive" in manifest["included_dirs"]
            assert manifest["manifest_version"] == 1
            assert manifest["app_version"]
            assert manifest["created_at"]
        finally:
            zip_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_copy_failure_is_recorded_as_partial(self, async_client: AsyncClient, data_dir, monkeypatch):
        """An injected copy failure still yields a ZIP — but a HONEST one."""
        from backend.app.api.routes.settings import _read_manifest_from_zip, create_backup_zip

        _write(data_dir / "archive" / "library" / "files" / "part.3mf", "payload")
        _write(data_dir / "icons" / "icon.png", "icon")

        real_copytree = shutil.copytree

        def flaky_copytree(src, dst, *args, **kwargs):
            if Path(src).name == "archive":
                # Real copytree creates the tree, then reports per-file failures.
                Path(dst).mkdir(parents=True, exist_ok=True)
                raise shutil.Error(
                    [(str(Path(src) / "library" / "files" / "part.3mf"), str(dst), "[Errno 13] Permission denied")]
                )
            return real_copytree(src, dst, *args, **kwargs)

        monkeypatch.setattr(shutil, "copytree", flaky_copytree)

        zip_path, _filename = await create_backup_zip(output_path=data_dir)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                assert "bambuddy.db" in zf.namelist()
                assert BACKUP_MANIFEST_NAME in zf.namelist()

            manifest = _read_manifest_from_zip(zip_path)
            assert manifest is not None
            assert manifest["partial"] is True
            assert [f["dir"] for f in manifest["failures"]] == ["archive"]
            assert manifest["failures"][0]["path"].endswith("part.3mf")
            assert "Permission denied" in manifest["failures"][0]["error"]
            assert "icons" in manifest["included_dirs"]
        finally:
            zip_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_backup_download_warns_when_partial(self, async_client: AsyncClient, data_dir, monkeypatch):
        _write(data_dir / "archive" / "library" / "files" / "part.3mf", "payload")

        real_copytree = shutil.copytree

        def flaky_copytree(src, dst, *args, **kwargs):
            if Path(src).name == "archive":
                Path(dst).mkdir(parents=True, exist_ok=True)
                raise shutil.Error([(str(src), str(dst), "[Errno 13] Permission denied")])
            return real_copytree(src, dst, *args, **kwargs)

        monkeypatch.setattr(shutil, "copytree", flaky_copytree)

        resp = await async_client.get("/api/v1/settings/backup")

        assert resp.status_code == 200
        assert resp.headers.get("x-backup-partial") == "true"
        assert "archive" in resp.headers.get("x-backup-warning", "")
        assert "INCOMPLETE" in resp.headers.get("x-backup-warning", "")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_backup_download_is_unflagged_when_complete(self, async_client: AsyncClient, data_dir):
        _write(data_dir / "archive" / "library" / "files" / "part.3mf", "payload")

        resp = await async_client.get("/api/v1/settings/backup")

        assert resp.status_code == 200
        assert "x-backup-partial" not in resp.headers


class TestRestorePartialRefusal:
    """A backup recorded as incomplete must not silently replace live data."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_partial_backup_is_refused_with_409(self, async_client: AsyncClient, data_dir):
        _write(data_dir / "archive" / "library" / "files" / "keep.3mf", "original")
        buf = _make_backup_zip(
            manifest=_partial_manifest(["archive"]),
            dirs={"archive": {"library/files/new.3mf": "from-backup"}},
        )

        with _mocked_db_restore():
            resp = await async_client.post(
                "/api/v1/settings/restore",
                files={"file": ("backup.zip", buf, "application/zip")},
            )

        assert resp.status_code == 409
        body = resp.json()
        assert body["success"] is False
        assert "INCOMPLETE" in body["message"]
        assert "archive" in body["message"]
        assert "force_partial=true" in body["message"]
        # Nothing touched.
        assert (data_dir / "archive" / "library" / "files" / "keep.3mf").read_text() == "original"
        assert not (data_dir / "archive" / "library" / "files" / "new.3mf").exists()
        assert _aside_dirs(data_dir) == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_partial_backup_restores_with_force_partial(self, async_client: AsyncClient, data_dir):
        _write(data_dir / "archive" / "library" / "files" / "keep.3mf", "original")
        buf = _make_backup_zip(
            manifest=_partial_manifest(["archive"]),
            dirs={"archive": {"library/files/new.3mf": "from-backup"}},
        )

        with _mocked_db_restore():
            resp = await async_client.post(
                "/api/v1/settings/restore?force_partial=true",
                files={"file": ("backup.zip", buf, "application/zip")},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["restored_partial"] is True
        assert "incomplete" in body["message"].lower()
        assert (data_dir / "archive" / "library" / "files" / "new.3mf").read_text() == "from-backup"
        assert not (data_dir / "archive" / "library" / "files" / "keep.3mf").exists()
        assert _aside_dirs(data_dir) == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unreadable_manifest_is_refused(self, async_client: AsyncClient, data_dir):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("bambuddy.db", b"SQLite format 3")
            zf.writestr(BACKUP_MANIFEST_NAME, b"{not json")
        buf.seek(0)

        with _mocked_db_restore():
            resp = await async_client.post(
                "/api/v1/settings/restore",
                files={"file": ("backup.zip", buf, "application/zip")},
            )

        assert resp.status_code == 409
        assert "unreadable" in resp.json()["message"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_manifestless_backup_is_allowed_but_noted(self, async_client: AsyncClient, data_dir):
        """Backups written before manifests existed still restore."""
        _write(data_dir / "archive" / "old.3mf", "original")
        buf = _make_backup_zip(manifest=None, dirs={"archive": {"new.3mf": "from-backup"}})

        with _mocked_db_restore():
            resp = await async_client.post(
                "/api/v1/settings/restore",
                files={"file": ("backup.zip", buf, "application/zip")},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["manifest_present"] is False
        assert "manifest" in body["message"]
        assert (data_dir / "archive" / "new.3mf").read_text() == "from-backup"


class TestRestoreTransactionality:
    """A failed restore must leave the destination exactly as it was."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_successful_restore_removes_aside_trees(self, async_client: AsyncClient, data_dir):
        _write(data_dir / "archive" / "library" / "files" / "keep.3mf", "original")
        _write(data_dir / "icons" / "old.png", "original-icon")
        buf = _make_backup_zip(
            manifest=_clean_manifest(["archive", "icons"]),
            dirs={"archive": {"library/files/new.3mf": "from-backup"}, "icons": {"new.png": "new-icon"}},
        )

        with _mocked_db_restore():
            resp = await async_client.post(
                "/api/v1/settings/restore",
                files={"file": ("backup.zip", buf, "application/zip")},
            )

        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert (data_dir / "archive" / "library" / "files" / "new.3mf").read_text() == "from-backup"
        assert (data_dir / "icons" / "new.png").read_text() == "new-icon"
        assert not (data_dir / "archive" / "library" / "files" / "keep.3mf").exists()
        assert not (data_dir / "icons" / "old.png").exists()
        assert _aside_dirs(data_dir) == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_copy_failure_mid_restore_rolls_every_dir_back(
        self, async_client: AsyncClient, data_dir, monkeypatch
    ):
        """The icons copy fails AFTER archive was replaced — both roll back."""
        _write(data_dir / "archive" / "library" / "files" / "keep.3mf", "original")
        _write(data_dir / "icons" / "old.png", "original-icon")
        buf = _make_backup_zip(
            manifest=_clean_manifest(["archive", "icons"]),
            dirs={"archive": {"library/files/new.3mf": "from-backup"}, "icons": {"new.png": "new-icon"}},
        )

        real_copy2 = shutil.copy2

        def flaky_copy2(src, dst, *args, **kwargs):
            if Path(dst).name == "new.png":
                raise OSError(13, "Permission denied", str(dst))
            return real_copy2(src, dst, *args, **kwargs)

        monkeypatch.setattr(shutil, "copy2", flaky_copy2)

        with _mocked_db_restore():
            resp = await async_client.post(
                "/api/v1/settings/restore",
                files={"file": ("backup.zip", buf, "application/zip")},
            )

        assert resp.status_code == 500
        # The invariant first: originals intact, backup content gone, no residue.
        assert (data_dir / "archive" / "library" / "files" / "keep.3mf").read_text() == "original"
        assert not (data_dir / "archive" / "library" / "files" / "new.3mf").exists()
        assert (data_dir / "icons" / "old.png").read_text() == "original-icon"
        assert not (data_dir / "icons" / "new.png").exists()
        assert _aside_dirs(data_dir) == []
        body = resp.json()
        assert body["success"] is False
        assert "icons" in body["message"]
        assert "rolled back" in body["message"]
        assert "database was not modified" in body["message"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_move_aside_failure_aborts_before_any_destruction(
        self, async_client: AsyncClient, data_dir, monkeypatch
    ):
        """A locked destination (Windows) must fail closed, not half-wipe."""
        import os

        _write(data_dir / "archive" / "library" / "files" / "keep.3mf", "original")
        buf = _make_backup_zip(
            manifest=_clean_manifest(["archive"]),
            dirs={"archive": {"library/files/new.3mf": "from-backup"}},
        )

        real_replace = os.replace

        def locked_replace(src, dst, *args, **kwargs):
            if ".pre-restore-" in str(dst):
                raise OSError(32, "The process cannot access the file because it is being used", str(src))
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", locked_replace)

        with _mocked_db_restore():
            resp = await async_client.post(
                "/api/v1/settings/restore",
                files={"file": ("backup.zip", buf, "application/zip")},
            )

        assert resp.status_code == 500
        assert "archive" in resp.json()["message"]
        assert (data_dir / "archive" / "library" / "files" / "keep.3mf").read_text() == "original"
        assert not (data_dir / "archive" / "library" / "files" / "new.3mf").exists()
        assert _aside_dirs(data_dir) == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_database_failure_rolls_dirs_back(self, async_client: AsyncClient, data_dir):
        """Dirs are restored before the DB, so a DB failure un-does them."""
        _write(data_dir / "archive" / "library" / "files" / "keep.3mf", "original")
        buf = _make_backup_zip(
            manifest=_clean_manifest(["archive"]),
            dirs={"archive": {"library/files/new.3mf": "from-backup"}},
        )

        failing_import = AsyncMock(side_effect=RuntimeError("database import blew up"))
        with _mocked_db_restore(import_pg=failing_import):
            resp = await async_client.post(
                "/api/v1/settings/restore",
                files={"file": ("backup.zip", buf, "application/zip")},
            )

        assert resp.status_code == 500
        assert (data_dir / "archive" / "library" / "files" / "keep.3mf").read_text() == "original"
        assert not (data_dir / "archive" / "library" / "files" / "new.3mf").exists()
        assert _aside_dirs(data_dir) == []
        body = resp.json()
        assert body["success"] is False
        assert "rolled back" in body["message"]
