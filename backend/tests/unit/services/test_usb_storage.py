"""The USB sweep (``usb_storage.sweep``) and the USB-drop alert.

ONE sweep body serves both triggers — the storage-low HMS code (``on_storage_low``) and every
print terminal (``sweep_after_terminal``) — over ONE FTPS session: the recording directories and
the root are listed, and every candidate is deleted on that same connection. What may go:
recordings and FARM-WRITTEN root files older than an hour, minus the keep-set
``job_terminal.live_upload_paths`` owns. Free space is never read (H2S refuses ``AVBL``).

FTPS is faked at the session boundary: ``usb_storage.run_in_session`` is patched to hand the
sweep's sync body a fake client IN A WORKER THREAD, exactly as production does — the body reads
its claim gate back on the event loop, which a body run on the loop thread itself would
deadlock. The live printer state is a fake manager; the DB is the test engine (patched onto
``core.database.async_session``). Notifications are asserted on the ``notification_service``
singleton.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import job_terminal, usb_storage, usb_uploads
from backend.app.services.bambu_ftp import BambuFTPClient, DeleteResult
from backend.app.services.notification_service import notification_service
from backend.app.services.plate_occupancy import plate_occupancy

NOW = 1_000_000.0
_FULL = {"0500010000030004"}
_GB = 1024**3
_STORAGE_LOW = SimpleNamespace(full_code="0500010000030004")


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #
class _FakeState:
    def __init__(
        self,
        state="IDLE",
        *,
        subtask_id=None,
        subtask_name=None,
        sdcard=False,
        hms_errors=None,
        connected=True,
    ):
        self.state = state
        self.subtask_id = subtask_id
        self.subtask_name = subtask_name
        self.sdcard = sdcard
        self.hms_errors = list(hms_errors or [])
        self.connected = connected


class _FakeClient:
    def __init__(self, state: _FakeState):
        self.state = state


class _FakeManager:
    """Injectable stand-in for printer_manager exposing get_client()/get_status()."""

    def __init__(self, client: _FakeClient | None = None):
        self._client = client

    def get_client(self, printer_id: int):
        return self._client

    def get_status(self, printer_id: int):
        return getattr(self._client, "state", None)


def _manager(state="IDLE", **kw) -> _FakeManager:
    return _FakeManager(_FakeClient(_FakeState(state, **kw)))


def _rec(name: str, *, path: str, size: int, age_s: float | None = 3 * 3600, is_dir: bool = False) -> dict:
    """A listing entry, its mtime ``age_s`` before NOW — UTC-aware, as ``list_files`` emits it."""
    entry: dict = {"name": name, "is_directory": is_dir, "size": size, "path": path}
    if age_s is not None:
        entry["mtime"] = datetime.fromtimestamp(NOW - age_s, tz=timezone.utc)
    return entry


class _FakeDrive:
    """The printer's drive behind ONE FTPS session: ``list_files`` / ``delete_file`` as the real
    client's sync methods, every session counted."""

    def __init__(self, listing: dict[str, list[dict]] | None = None, *, unreachable: bool = False):
        self.listing = listing or {}
        self.unreachable = unreachable
        self.sessions = 0
        self.listed: list[str] = []
        self.deleted: list[str] = []
        self.results: dict[str, DeleteResult] = {}
        self.on_delete = None  # a hook run before each delete (the claim-mid-sweep tests)

    def list_files(self, path: str) -> list[dict]:
        self.listed.append(path)
        return list(self.listing.get(path, []))

    def delete_file(self, path: str) -> DeleteResult:
        if self.on_delete is not None:
            self.on_delete(path)
        self.deleted.append(path)
        return self.results.get(path, DeleteResult.DELETED)

    async def run_in_session(self, ip, access_code, fn, *, socket_timeout=None, printer_model=None):
        if self.unreachable:
            raise ConnectionError("FTPS connect/login failed")
        self.sessions += 1
        return await asyncio.to_thread(fn, self)


@pytest.fixture(autouse=True)
def _clean_state():
    usb_storage._reset_state()
    usb_uploads._reset_state()
    yield
    usb_storage._reset_state()
    usb_uploads._reset_state()


@pytest.fixture
def session_maker(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


def _patch_session(session_maker):
    return patch("backend.app.core.database.async_session", session_maker)


class _Env:
    """Everything a sweep reaches: the DB, the drive and the notification."""

    def __init__(self, session_maker, drive: _FakeDrive):
        self._session_maker = session_maker
        self.drive = drive
        self.notify: AsyncMock | None = None
        self._stack: list = []

    def __enter__(self):
        self._stack = [
            _patch_session(self._session_maker),
            patch.object(usb_storage, "run_in_session", new=self.drive.run_in_session),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock),
        ]
        entered = [cm.__enter__() for cm in self._stack]
        self.notify = entered[2]
        return self

    def __exit__(self, *exc):
        for cm in reversed(self._stack):
            cm.__exit__(*exc)


async def _archive(db, printer_id: int, filename: str) -> PrintArchive:
    archive = PrintArchive(
        printer_id=printer_id,
        filename=filename,
        print_name=filename,
        file_path=f"archives/x/{filename}",
        file_size=123,
        status="completed",
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)
    return archive


async def _library(db, filename: str) -> LibraryFile:
    lib = LibraryFile(filename=filename, file_path=f"/lib/{filename}", file_type="3mf", file_size=1, is_external=True)
    db.add(lib)
    await db.commit()
    await db.refresh(lib)
    return lib


def _no_sleep():
    return AsyncMock()


# --------------------------------------------------------------------------- #
# Gates: setting / printing / cooldown / claim / single-flight
# --------------------------------------------------------------------------- #
class TestGates:
    async def test_disabled_setting_is_a_no_op_for_both_triggers(self, db_session, session_maker, printer_factory):
        from backend.app.api.routes.settings import set_setting

        printer = await printer_factory(model="H2S")
        await set_setting(db_session, "farm_usb_auto_cleanup", "false")
        await db_session.commit()

        with _Env(session_maker, _FakeDrive()) as env:
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_manager(), now=NOW)
            await usb_storage.sweep_after_terminal(printer.id, manager=_manager(), now=NOW)

        assert env.drive.sessions == 0
        env.notify.assert_not_awaited()
        # A disabled feature is not a deferral: retrying it would spin a task every tick.
        assert printer.id not in usb_storage._deferred_printers
        assert printer.id not in usb_storage._last_cleanup_at

    @pytest.mark.parametrize("live", ["RUNNING", "prepare"])
    async def test_printing_defers_an_hms_trigger_without_burning_the_cooldown(
        self, db_session, session_maker, printer_factory, live
    ):
        printer = await printer_factory(model="H2S")
        with _Env(session_maker, _FakeDrive()) as env:
            assert await usb_storage.sweep(printer.id, trigger="hms", manager=_manager(live), now=NOW) == "printing"

        assert env.drive.sessions == 0
        env.notify.assert_not_awaited()
        assert printer.id in usb_storage._deferred_printers
        assert printer.id not in usb_storage._last_cleanup_at

    async def test_cooldown_defers_an_hms_trigger(self, db_session, session_maker, printer_factory):
        """2026-10-05: a code arriving inside the old shared 6 h cooldown returned "cooldown",
        dropped the deferral and never retried — 237 storage-low hits on 013-H2S, zero pages."""
        printer = await printer_factory(model="H2S")
        usb_storage._last_cleanup_at[printer.id] = NOW
        with _Env(session_maker, _FakeDrive()) as env:
            verdict = await usb_storage.sweep(
                printer.id, trigger="hms", manager=_manager(), now=NOW + usb_storage.CLEANUP_COOLDOWN_S - 1
            )

        assert verdict == "cooldown"
        assert env.drive.sessions == 0
        assert printer.id in usb_storage._deferred_printers

    async def test_a_dispatch_lease_or_eject_in_flight_defers_an_hms_trigger(
        self, db_session, session_maker, printer_factory
    ):
        printer = await printer_factory(model="H2S")
        with (
            _Env(session_maker, _FakeDrive()) as env,
            patch.object(plate_occupancy, "printers_with_lease_or_eject", return_value={printer.id}),
        ):
            verdict = await usb_storage.sweep(printer.id, trigger="hms", manager=_manager(), now=NOW)

        assert verdict == "claimed"
        assert env.drive.sessions == 0
        assert printer.id in usb_storage._deferred_printers

    async def test_an_upload_in_flight_defers_an_hms_trigger(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        with _Env(session_maker, _FakeDrive()) as env:
            async with usb_uploads.upload_in_flight(printer.id):
                verdict = await usb_storage.sweep(printer.id, trigger="hms", manager=_manager(), now=NOW)

        assert verdict == "claimed"
        assert env.drive.sessions == 0
        assert printer.id in usb_storage._deferred_printers

    async def test_a_swallowed_terminal_trigger_leaves_no_deferral(self, db_session, session_maker, printer_factory):
        """The terminal trigger waits for the next terminal; only the HMS trigger owes a retry."""
        printer = await printer_factory(model="H2S")
        usb_storage._last_cleanup_at[printer.id] = NOW
        with _Env(session_maker, _FakeDrive()) as env:
            assert await usb_storage.sweep(printer.id, trigger="terminal", manager=_manager("RUNNING"), now=NOW) == (
                "printing"
            )
            assert await usb_storage.sweep(printer.id, trigger="terminal", manager=_manager(), now=NOW + 60) == (
                "cooldown"
            )

        assert env.drive.sessions == 0
        assert printer.id not in usb_storage._deferred_printers

    async def test_single_flight_collapses_concurrent_triggers(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        usb_storage._inflight.add(printer.id)
        with _Env(session_maker, _FakeDrive()) as env:
            await usb_storage.sweep_after_terminal(printer.id, manager=_manager(), now=NOW)
            assert printer.id not in usb_storage._deferred_printers
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_manager(), now=NOW)

        assert env.drive.sessions == 0
        env.notify.assert_not_awaited()
        # The HMS trigger the running sweep swallowed is owed a retry of its own.
        assert printer.id in usb_storage._deferred_printers

    async def test_single_flight_is_released_after_the_sweep(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        with _Env(session_maker, _FakeDrive()):
            await usb_storage.sweep_after_terminal(printer.id, manager=_manager(), now=NOW)
        assert printer.id not in usb_storage._inflight

    async def test_only_storage_low_codes_trigger(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        with _Env(session_maker, _FakeDrive()) as env:
            await usb_storage.on_storage_low(printer.id, {"0300400C00010001"}, manager=_manager(), now=NOW)
        assert env.drive.sessions == 0
        assert printer.id not in usb_storage._last_cleanup_at

    def test_the_cooldowns(self):
        """The sweep cooldown is short enough for a deferral to retry within minutes; the
        USB-drop alert keeps its own, unchanged 6 h window."""
        assert usb_storage.CLEANUP_COOLDOWN_S == 600.0
        assert usb_storage.USB_DROP_ALERT_COOLDOWN_S == 6 * 3600.0


# --------------------------------------------------------------------------- #
# The deferral: ONE writer set, one remover, a cooldown-aware retry predicate
# --------------------------------------------------------------------------- #
class TestDeferral:
    def test_should_retry_deferred_predicate(self):
        pid = 7
        assert usb_storage.should_retry_deferred(pid, "IDLE", now=NOW) is False  # nothing deferred

        usb_storage._deferred_printers.add(pid)
        # Deferred + idle → retry, INDEPENDENT of HMS presence: the firmware drops the storage
        # code from hms[] when it unmounts a full drive (the printer-7 silent stall).
        assert usb_storage.should_retry_deferred(pid, "IDLE", now=NOW) is True
        assert usb_storage.should_retry_deferred(pid, None, now=NOW) is True
        assert usb_storage.should_retry_deferred(pid, "FINISH", now=NOW) is True
        assert usb_storage.should_retry_deferred(pid, "RUNNING", now=NOW) is False
        assert usb_storage.should_retry_deferred(pid, "prepare", now=NOW) is False
        assert pid in usb_storage._deferred_printers  # pure

    def test_should_retry_deferred_waits_for_the_cooldown(self):
        pid = 8
        usb_storage._deferred_printers.add(pid)
        usb_storage._last_cleanup_at[pid] = NOW
        assert usb_storage.should_retry_deferred(pid, "IDLE", now=NOW + 60) is False
        assert usb_storage.should_retry_deferred(pid, "IDLE", now=NOW + usb_storage.CLEANUP_COOLDOWN_S) is True

    def test_take_deferred_is_the_one_remover(self):
        pid = 9
        assert usb_storage.take_deferred(pid) is False
        usb_storage._deferred_printers.add(pid)
        assert usb_storage.take_deferred(pid) is True
        assert usb_storage.take_deferred(pid) is False
        assert pid not in usb_storage._deferred_printers

    async def test_the_retry_after_the_print_sweeps_and_leaves_no_deferral(
        self, db_session, session_maker, printer_factory
    ):
        printer = await printer_factory(model="H2S")
        drive = _FakeDrive({"/ipcam": [_rec("old.mp4", path="/ipcam/old.mp4", size=1_000_000, age_s=2 * 3600)]})
        with _Env(session_maker, drive) as env:
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_manager("RUNNING"), now=NOW)
            assert drive.deleted == []
            assert printer.id in usb_storage._deferred_printers

            # main.py's hook: the predicate, then the one remover, then the retry.
            assert usb_storage.should_retry_deferred(printer.id, "FINISH", now=NOW + 120)
            assert usb_storage.take_deferred(printer.id)
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_manager("FINISH"), now=NOW + 120)

        assert drive.deleted == ["/ipcam/old.mp4"]
        env.notify.assert_not_awaited()  # the code is not standing — nothing to say
        assert printer.id not in usb_storage._deferred_printers
        assert usb_storage._last_cleanup_at[printer.id] == NOW + 120

    async def test_a_retry_against_an_unmounted_drive_pages_unreachable(
        self, db_session, session_maker, printer_factory
    ):
        """The printer-7 fix end to end: a sweep deferred while printing, retried once idle
        against a now-UNMOUNTED drive (hms[] cleared), pages instead of staying silent."""
        printer = await printer_factory(model="H2S")
        with _Env(session_maker, _FakeDrive(unreachable=True)) as env:
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_manager("RUNNING"), now=NOW)
            env.notify.assert_not_awaited()
            assert usb_storage.take_deferred(printer.id)
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_manager(), now=NOW + 120)

        env.notify.assert_awaited_once()
        kwargs = env.notify.await_args.kwargs
        assert kwargs["success"] is False
        assert "FTPS unreachable" in kwargs["reason"]
        assert printer.id not in usb_storage._deferred_printers

    async def test_an_unreachable_drive_never_pages_from_the_terminal_trigger(
        self, db_session, session_maker, printer_factory
    ):
        printer = await printer_factory(model="H2S")
        with _Env(session_maker, _FakeDrive(unreachable=True)) as env:
            assert await usb_storage.sweep(printer.id, trigger="terminal", manager=_manager(), now=NOW) == (
                "unreachable"
            )
        env.notify.assert_not_awaited()


# --------------------------------------------------------------------------- #
# ONE session, both stages, for both triggers
# --------------------------------------------------------------------------- #
class TestOneSessionBothStages:
    @pytest.mark.parametrize("trigger", ["hms", "terminal"])
    async def test_both_stages_run_in_one_session(self, db_session, session_maker, printer_factory, trigger, caplog):
        printer = await printer_factory(model="H2S")
        await _archive(db_session, printer.id, "Bracket v2.gcode.3mf")  # remote "Bracket_v2.3mf"
        drive = _FakeDrive(
            {
                "/ipcam": [_rec("rec.mp4", path="/ipcam/rec.mp4", size=250_000_000, age_s=4 * 3600)],
                "/ipcam/thumbnail": [_rec("rec.jpg", path="/ipcam/thumbnail/rec.jpg", size=50_000, age_s=4 * 3600)],
                "/timelapse": [_rec("tl.mp4", path="/timelapse/tl.mp4", size=10_000_000, age_s=30 * 3600)],
                "/timelapse/thumbnail": [_rec("tl.jpg", path="/timelapse/thumbnail/tl.jpg", size=20_000)],
                "/": [_rec("Bracket_v2.3mf", path="/Bracket_v2.3mf", size=23_000_000, age_s=30 * 3600)],
            }
        )
        with caplog.at_level(logging.INFO, logger="backend.app.services.usb_storage"), _Env(session_maker, drive):
            assert await usb_storage.sweep(printer.id, trigger=trigger, manager=_manager(), now=NOW) == "done"

        assert drive.sessions == 1
        assert drive.listed == ["/ipcam", "/ipcam/thumbnail", "/timelapse", "/timelapse/thumbnail", "/"]
        # Recordings first (the bulk), then the farm-written root file.
        assert drive.deleted == [
            "/ipcam/rec.mp4",
            "/ipcam/thumbnail/rec.jpg",
            "/timelapse/tl.mp4",
            "/timelapse/thumbnail/tl.jpg",
            "/Bracket_v2.3mf",
        ]
        messages = [r.getMessage() for r in caplog.records]
        assert "[USB-STORAGE] deleted recording /ipcam/rec.mp4 (250000000 bytes)" in messages
        assert "[USB-STORAGE] deleted stale print file /Bracket_v2.3mf (23000000 bytes)" in messages
        assert (
            f"[USB-STORAGE] printer {printer.id} cleanup done: freed 283070000 bytes across 5 file(s), success=True"
            in messages
        )
        assert usb_storage._last_cleanup_at[printer.id] == NOW

    async def test_stage_two_runs_with_free_space_unknowable(self, db_session, session_maker, printer_factory):
        """H2S refuses FTP AVBL, so a stage 2 gated on known-low free space never ran on an H2S
        (001-H2S: 52 stale root files). The sweep no longer asks for free space at all."""
        assert not hasattr(usb_storage, "get_storage_info_async")
        assert not hasattr(usb_storage, "TARGET_FREE_BYTES")
        assert not hasattr(usb_storage, "_free_bytes_from")

        printer = await printer_factory(model="H2S")
        await _library(db_session, "Clip.gcode.3mf")
        drive = _FakeDrive({"/": [_rec("Clip.3mf", path="/Clip.3mf", size=5 * _GB, age_s=10 * 3600)]})
        with _Env(session_maker, drive):
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_manager(), now=NOW)

        assert drive.deleted == ["/Clip.3mf"]


# --------------------------------------------------------------------------- #
# Scope: farm-written names, the age floor, the keep-set
# --------------------------------------------------------------------------- #
class TestScope:
    async def test_only_farm_written_root_files_leave_the_drive(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        await _archive(db_session, printer.id, "farm part.gcode.3mf")
        await _library(db_session, "Lib Thing.3mf")
        old = 10 * 3600
        drive = _FakeDrive(
            {
                "/": [
                    _rec("farm_part.3mf", path="/farm_part.3mf", size=1, age_s=old),  # archive's remote name
                    _rec("LIB_THING.3MF", path="/LIB_THING.3MF", size=1, age_s=old),  # library's, case-folded
                    _rec("eject_production_item2043.3mf", path="/eject_production_item2043.3mf", size=1, age_s=old),
                    _rec("eject_manual_p4.3mf", path="/eject_manual_p4.3mf", size=1, age_s=old),
                    _rec("DRY-RUN_single-pass.3mf", path="/DRY-RUN_single-pass.3mf", size=1, age_s=old),
                    _rec("Widget.gcode.3mf", path="/Widget.gcode.3mf", size=1, age_s=old),  # Studio — never
                    _rec("verify_job", path="/verify_job", size=1, age_s=old),  # firmware's — never
                    _rec("model", path="/model", size=0, age_s=old, is_dir=True),  # a directory — never
                ]
            }
        )
        with _Env(session_maker, drive):
            await usb_storage.sweep(printer.id, trigger="terminal", manager=_manager(), now=NOW)

        assert drive.deleted == [
            "/farm_part.3mf",
            "/LIB_THING.3MF",
            "/eject_production_item2043.3mf",
            "/eject_manual_p4.3mf",
            "/DRY-RUN_single-pass.3mf",
        ]

    async def test_nothing_younger_than_the_floor_and_nothing_undated(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        await _archive(db_session, printer.id, "fresh.gcode.3mf")
        drive = _FakeDrive(
            {
                "/ipcam": [
                    _rec("old.mp4", path="/ipcam/old.mp4", size=1, age_s=2 * 3600),
                    _rec("writing.mp4", path="/ipcam/writing.mp4", size=1, age_s=60),
                    _rec("nostamp.mp4", path="/ipcam/nostamp.mp4", size=1, age_s=None),
                    _rec("sub", path="/ipcam/sub", size=0, is_dir=True),
                ],
                "/": [_rec("fresh.3mf", path="/fresh.3mf", size=1, age_s=30 * 60)],
            }
        )
        with _Env(session_maker, drive):
            await usb_storage.sweep(printer.id, trigger="terminal", manager=_manager(), now=NOW)

        assert drive.deleted == ["/ipcam/old.mp4"]

    async def test_a_recording_30_min_old_in_utc_stays_and_one_2_h_old_goes(
        self, db_session, session_maker, printer_factory
    ):
        """Through the REAL listing parser: the printer stamps LIST in UTC, so the recording
        being written reads minutes old whatever the farm's local zone is (2026-10-04 23:46: a
        110 MB partial deleted because a local-time parse dated it last year)."""
        printer = await printer_factory(model="H2S")
        utc_now = datetime.now(timezone.utc)

        def _line(name: str, age: timedelta) -> str:
            stamp = utc_now - age
            return f"-rw-r--r-- 1 root root 1000 {stamp:%b} {stamp.day:2d} {stamp:%H:%M} {name}"

        lines = {
            "/ipcam": [_line("rec_new.mp4", timedelta(minutes=30)), _line("rec_old.mp4", timedelta(hours=2))],
        }
        deleted: list[str] = []
        state = SimpleNamespace(cwd=None)

        def _cwd(path):
            state.cwd = path

        client = BambuFTPClient("127.0.0.1", "x")
        client._ftp = SimpleNamespace(
            cwd=_cwd,
            retrlines=lambda _cmd, callback: [callback(line) for line in lines.get(state.cwd, [])],
            delete=deleted.append,
        )

        async def _session(ip, access_code, fn, *, socket_timeout=None, printer_model=None):
            return await asyncio.to_thread(fn, client)

        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "run_in_session", new=_session),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock),
        ):
            await usb_storage.sweep(printer.id, trigger="terminal", manager=_manager())

        assert deleted == ["/ipcam/rec_old.mp4"]

    async def test_the_keep_set_is_job_terminals(self, db_session, session_maker, printer_factory):
        """The live job's paths and the PENDING units pinned to this printer stay; a pool unit's
        file (no pin — it may never print here) and a finished job's file go."""
        printer = await printer_factory(model="H2S")
        pinned = await _archive(db_session, printer.id, "pinned.gcode.3mf")
        pool = await _archive(db_session, printer.id, "pool.gcode.3mf")
        await _archive(db_session, printer.id, "livejob.gcode.3mf")
        await _archive(db_session, printer.id, "done.gcode.3mf")
        db_session.add_all(
            [
                PrintQueueItem(printer_id=printer.id, status="pending", archive_id=pinned.id, plate_id=1, position=1),
                PrintQueueItem(printer_id=None, status="pending", archive_id=pool.id, plate_id=1, position=2),
            ]
        )
        await db_session.commit()

        old = 10 * 3600
        drive = _FakeDrive(
            {
                "/": [
                    _rec("pinned.3mf", path="/pinned.3mf", size=1, age_s=old),
                    _rec("pool.3mf", path="/pool.3mf", size=1, age_s=old),
                    _rec("livejob.3mf", path="/livejob.3mf", size=1, age_s=old),
                    _rec("done.3mf", path="/done.3mf", size=1, age_s=old),
                ]
            }
        )
        keep_set = AsyncMock(side_effect=job_terminal.live_upload_paths)
        live = _manager("PAUSE", subtask_id="", subtask_name="livejob")
        with _Env(session_maker, drive), patch.object(usb_storage, "live_upload_paths", keep_set):
            await usb_storage.sweep(printer.id, trigger="terminal", manager=live, now=NOW)

        assert drive.deleted == ["/pool.3mf", "/done.3mf"]
        keep_set.assert_awaited_once()
        assert keep_set.await_args.args[1:] == (printer.id, "", "livejob")

    def test_the_old_in_use_reader_is_gone(self):
        """ONE keep-set owner: the sweep's own name-variant reader was a second answer."""
        assert not hasattr(usb_storage, "_in_use_remote_names")
        assert not hasattr(usb_storage, "_name_variants")
        assert not hasattr(usb_storage, "drain_recordings_if_idle")
        assert not hasattr(usb_storage, "has_deferred")


# --------------------------------------------------------------------------- #
# A claim that arrives mid-sweep stops it
# --------------------------------------------------------------------------- #
class TestClaimMidSweep:
    async def test_an_upload_starting_mid_sweep_stops_the_deletes(self, db_session, session_maker, printer_factory):
        """A dispatch re-uploads the same name a finished unit left behind; the gate is re-read
        before every delete so the sweep can never delete the file being uploaded."""
        printer = await printer_factory(model="H2S")
        await _archive(db_session, printer.id, "part.gcode.3mf")
        drive = _FakeDrive(
            {
                "/ipcam": [_rec("a.mp4", path="/ipcam/a.mp4", size=10, age_s=2 * 3600)],
                "/": [_rec("part.3mf", path="/part.3mf", size=10, age_s=2 * 3600)],
            }
        )
        # The dispatcher's upload registers right after the first delete lands.
        drive.on_delete = lambda _path: usb_uploads._uploads_in_flight.add(printer.id)

        sleep = _no_sleep()
        manager = _manager(hms_errors=[_STORAGE_LOW])
        with _Env(session_maker, drive) as env:
            assert await usb_storage.sweep(printer.id, trigger="hms", manager=manager, now=NOW, sleep=sleep) == "done"

        assert drive.deleted == ["/ipcam/a.mp4"]
        # The HMS trigger is owed the rest: its deferral is re-armed, and the verdict (the settle
        # wait, any page) belongs to the sweep that finishes the drive.
        assert printer.id in usb_storage._deferred_printers
        sleep.assert_not_awaited()
        env.notify.assert_not_awaited()

    async def test_a_lease_minted_mid_sweep_stops_the_deletes(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        drive = _FakeDrive(
            {
                "/ipcam": [
                    _rec("a.mp4", path="/ipcam/a.mp4", size=10, age_s=2 * 3600),
                    _rec("b.mp4", path="/ipcam/b.mp4", size=10, age_s=2 * 3600),
                ]
            }
        )
        claimed: set[int] = set()
        drive.on_delete = lambda _path: claimed.add(printer.id)
        with (
            _Env(session_maker, drive),
            patch.object(plate_occupancy, "printers_with_lease_or_eject", side_effect=lambda: set(claimed)),
        ):
            await usb_storage.sweep(printer.id, trigger="terminal", manager=_manager(), now=NOW)

        assert drive.deleted == ["/ipcam/a.mp4"]
        assert printer.id not in usb_storage._deferred_printers


# --------------------------------------------------------------------------- #
# The HMS trigger's verdict: the settle check and the failure pages
# --------------------------------------------------------------------------- #
class TestStorageLowVerdict:
    @staticmethod
    def _drive() -> _FakeDrive:
        return _FakeDrive({"/ipcam": [_rec("rec.mp4", path="/ipcam/rec.mp4", size=300 * 1024 * 1024, age_s=7200)]})

    async def test_success_is_silent_once_the_code_drops(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        manager = _manager(hms_errors=[_STORAGE_LOW])
        polls: list[float] = []

        async def _sleep(seconds):
            polls.append(seconds)
            manager.get_status(printer.id).hms_errors.clear()  # the firmware re-evaluates

        with _Env(session_maker, self._drive()) as env:
            await usb_storage.on_storage_low(printer.id, _FULL, manager=manager, now=NOW, sleep=_sleep)

        env.notify.assert_not_awaited()
        assert polls == [usb_storage.STORAGE_SETTLE_POLL_S]

    async def test_the_code_still_standing_pages_once_with_the_operator_exit(
        self, db_session, session_maker, printer_factory
    ):
        printer = await printer_factory(model="H2S")
        sleep = _no_sleep()
        with _Env(session_maker, self._drive()) as env:
            await usb_storage.on_storage_low(
                printer.id, _FULL, manager=_manager(hms_errors=[_STORAGE_LOW]), now=NOW, sleep=sleep
            )

        env.notify.assert_awaited_once()
        kwargs = env.notify.await_args.kwargs
        assert kwargs["success"] is False
        assert kwargs["files_deleted"] == 1
        assert kwargs["freed_bytes"] == 300 * 1024 * 1024
        assert kwargs["reason"] == usb_storage.STORAGE_STILL_LOW_REASON
        assert "free_bytes" not in kwargs
        # The wait is the measured settle window, polled in memory.
        assert sleep.await_count == int(usb_storage.STORAGE_SETTLE_S / usb_storage.STORAGE_SETTLE_POLL_S)

    def test_the_settle_window_is_the_measured_one(self):
        """2x the longer of the two live measurements, and the constant says so."""
        import inspect

        assert usb_storage.STORAGE_SETTLE_S == 60.0
        prose = " ".join(inspect.getsource(usb_storage).replace("#", " ").split())
        assert (
            "the firmware dropped the code 19 s and 32 s after the deletes on 014-H2S / 013-H2S, 2026-10-05; "
            "2x the longer one" in prose
        )

    async def test_the_terminal_trigger_never_waits_and_never_pages(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        sleep = _no_sleep()
        with _Env(session_maker, self._drive()) as env:
            await usb_storage.sweep(
                printer.id, trigger="terminal", manager=_manager(hms_errors=[_STORAGE_LOW]), now=NOW, sleep=sleep
            )
        env.notify.assert_not_awaited()
        sleep.assert_not_awaited()

    async def test_nothing_cleanable_while_the_code_stands_pages(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        with _Env(session_maker, _FakeDrive()) as env:
            await usb_storage.on_storage_low(
                printer.id, _FULL, manager=_manager(hms_errors=[_STORAGE_LOW]), now=NOW, sleep=_no_sleep()
            )
        env.notify.assert_awaited_once()
        kwargs = env.notify.await_args.kwargs
        assert kwargs["files_deleted"] == 0
        # The 4 GB + video-recording case: the drive holds only what the printer keeps writing.
        assert kwargs["reason"] == f"nothing left to clean; {usb_storage.STORAGE_STILL_LOW_REASON}"

    async def test_nothing_cleanable_and_the_code_gone_is_silent(self, db_session, session_maker, printer_factory):
        """A deferred retry no longer requires the code (it can vanish on an unmount); one that
        finds a reachable, clean drive and no code has nothing to report."""
        printer = await printer_factory(model="H2S")
        with _Env(session_maker, _FakeDrive()) as env:
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_manager(), now=NOW, sleep=_no_sleep())
        env.notify.assert_not_awaited()

    async def test_undeletable_files_page(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        drive = self._drive()
        drive.results["/ipcam/rec.mp4"] = DeleteResult.FAILED
        with _Env(session_maker, drive) as env:
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_manager(), now=NOW, sleep=_no_sleep())
        env.notify.assert_awaited_once()
        assert env.notify.await_args.kwargs["reason"] == "1 file(s) could not be deleted"

    async def test_a_standing_condition_pages_once_per_window(self, db_session, session_maker, printer_factory, caplog):
        """A 4 GB drive with recording on raises the code at every print; the 10 min sweep
        cooldown lets each one sweep, and only ONE page per printer per 6 h goes out."""
        printer = await printer_factory(model="H2S")
        manager = _manager(hms_errors=[_STORAGE_LOW])
        with (
            caplog.at_level(logging.INFO, logger="backend.app.services.usb_storage"),
            _Env(session_maker, _FakeDrive()) as env,
        ):
            await usb_storage.on_storage_low(printer.id, _FULL, manager=manager, now=NOW, sleep=_no_sleep())
            second = NOW + 20 * 60
            await usb_storage.on_storage_low(printer.id, _FULL, manager=manager, now=second, sleep=_no_sleep())
            assert env.drive.sessions == 2  # both swept: the page window is not the sweep cooldown
            assert env.notify.await_count == 1
            assert (
                f"[USB-STORAGE] printer {printer.id} storage-low page within its cooldown; suppressed "
                f"(nothing left to clean; {usb_storage.STORAGE_STILL_LOW_REASON})"
            ) in [r.getMessage() for r in caplog.records]

            later = NOW + usb_storage.STORAGE_LOW_PAGE_COOLDOWN_S + 1
            await usb_storage.on_storage_low(printer.id, _FULL, manager=manager, now=later, sleep=_no_sleep())
        assert env.notify.await_count == 2
        assert usb_storage._last_storage_page_at[printer.id] == later

    async def test_every_sweep_page_shares_the_window(self, db_session, session_maker, printer_factory):
        """An unreachable drive after a still-low page is the same standing condition."""
        printer = await printer_factory(model="H2S")
        with _Env(session_maker, self._drive()) as env:
            await usb_storage.on_storage_low(
                printer.id, _FULL, manager=_manager(hms_errors=[_STORAGE_LOW]), now=NOW, sleep=_no_sleep()
            )
        assert env.notify.await_args.kwargs["reason"] == usb_storage.STORAGE_STILL_LOW_REASON
        with _Env(session_maker, _FakeDrive(unreachable=True)) as env:
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_manager(), now=NOW + 3600, sleep=_no_sleep())
        env.notify.assert_not_awaited()

    def test_the_page_window(self):
        assert usb_storage.STORAGE_LOW_PAGE_COOLDOWN_S == 6 * 3600.0

    async def test_the_drop_alert_is_not_held_by_the_page_window(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        with _Env(session_maker, _FakeDrive()) as env:
            await usb_storage.on_storage_low(
                printer.id, _FULL, manager=_manager(hms_errors=[_STORAGE_LOW]), now=NOW, sleep=_no_sleep()
            )
            with patch.object(usb_storage, "list_files_async", new=_list_from({"/": []})):
                await usb_storage.verify_and_alert_usb_drop(
                    printer.id, manager=_FakeManager(), now=NOW + 60, sleep=AsyncMock()
                )
        assert env.notify.await_count == 2
        assert env.notify.await_args.kwargs["reason"] == usb_storage.USB_DROP_REASON
        assert env.notify.await_args.kwargs["attempted"] is False

    async def test_an_unexpected_failure_pages_and_never_raises(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        drive = self._drive()

        async def _boom(*_a, **_kw):
            raise RuntimeError("worker died")

        drive.run_in_session = _boom
        with _Env(session_maker, drive) as env:
            assert await usb_storage.sweep(printer.id, trigger="hms", manager=_manager(), now=NOW) == "error"
        env.notify.assert_awaited_once()
        assert env.notify.await_args.kwargs["reason"] == "unexpected error during cleanup"
        assert printer.id not in usb_storage._inflight


# --------------------------------------------------------------------------- #
# The USB-drop edge and its verify-then-alert lane
# --------------------------------------------------------------------------- #
class TestSdcardDrop:
    async def test_never_fires_from_startup_default(self):
        # Never-reported → default False observed: no drop, and we don't even record
        # it (so a later True is a clean first observation).
        assert usb_storage.record_sdcard_and_detect_drop(1, False) is False
        assert 1 not in usb_storage._last_sdcard

    async def test_fires_once_on_genuine_transition(self):
        assert usb_storage.record_sdcard_and_detect_drop(2, True) is False
        assert usb_storage.record_sdcard_and_detect_drop(2, False) is True
        assert usb_storage.record_sdcard_and_detect_drop(2, False) is False

    async def test_rearms_after_remount(self):
        assert usb_storage.record_sdcard_and_detect_drop(3, True) is False
        assert usb_storage.record_sdcard_and_detect_drop(3, False) is True
        assert usb_storage.record_sdcard_and_detect_drop(3, True) is False
        assert usb_storage.record_sdcard_and_detect_drop(3, False) is True


def _list_from(mapping: dict[str, list[dict]]):
    async def _list(ip, access_code, path="/", printer_model=None, **kw):
        return list(mapping.get(path, []))

    return AsyncMock(side_effect=_list)


class TestVerifyAndAlertUsbDrop:
    """The verify-then-alert flow behind main.py's ``sdcard`` True→False edge: upload-in-flight
    suppression, an FTPS re-probe that filters ~1 s dispatch blips, live-state cancellation, and
    a per-printer 6 h alert cooldown. Only a probe-confirmed drop fires the ``attempted=False``
    notification."""

    async def test_edge_during_upload_is_suppressed(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        usb_uploads._uploads_in_flight.add(printer.id)
        list_mock = _list_from({"/": []})
        sleep = AsyncMock()
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.verify_and_alert_usb_drop(printer.id, manager=_FakeManager(), now=NOW, sleep=sleep)
        notify.assert_not_awaited()
        list_mock.assert_not_awaited()
        sleep.assert_not_awaited()

    async def test_probe_sees_usb_present_suppressed(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        list_mock = _list_from({"/": [_rec("a.3mf", path="/a.3mf", size=_GB)]})
        sleep = AsyncMock()
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.verify_and_alert_usb_drop(printer.id, manager=_FakeManager(), now=NOW, sleep=sleep)
        notify.assert_not_awaited()
        sleep.assert_not_awaited()
        assert printer.id not in usb_storage._last_drop_alert_at

    async def test_confirmed_drop_alerts_once(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        list_mock = _list_from({"/": []})
        sleep = AsyncMock()
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.verify_and_alert_usb_drop(printer.id, manager=_FakeManager(), now=NOW, sleep=sleep)
        sleep.assert_awaited_once()
        notify.assert_awaited_once()
        kwargs = notify.await_args.kwargs
        assert kwargs["attempted"] is False
        assert kwargs["success"] is False
        assert kwargs["reason"] == usb_storage.USB_DROP_REASON
        assert usb_storage._last_drop_alert_at[printer.id] == NOW

    async def test_upload_starting_after_first_probe_is_suppressed(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        list_mock = _list_from({"/": []})

        async def _sleep(_seconds):
            usb_uploads._uploads_in_flight.add(printer.id)

        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.verify_and_alert_usb_drop(printer.id, manager=_FakeManager(), now=NOW, sleep=_sleep)
        notify.assert_not_awaited()
        assert printer.id not in usb_storage._last_drop_alert_at

    async def test_probe_exception_treated_as_absent_and_alerts(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        raising = AsyncMock(side_effect=OSError("port 990 down"))
        sleep = AsyncMock()
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "list_files_async", new=raising),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.verify_and_alert_usb_drop(printer.id, manager=_FakeManager(), now=NOW, sleep=sleep)
        notify.assert_awaited_once()
        assert notify.await_args.kwargs["attempted"] is False

    async def test_sdcard_restored_during_verification_cancels(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        list_mock = _list_from({"/": []})
        mgr = _manager(sdcard=True)
        sleep = AsyncMock()
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.verify_and_alert_usb_drop(printer.id, manager=mgr, now=NOW, sleep=sleep)
        notify.assert_not_awaited()
        assert printer.id not in usb_storage._last_drop_alert_at

    async def test_cooldown_suppresses_second_drop_then_fires_after(self, db_session, session_maker, printer_factory):
        """The drop lane keeps its own 6 h window — unchanged by the sweep's 10 min cooldown."""
        printer = await printer_factory(model="H2S")
        list_mock = _list_from({"/": []})
        sleep = AsyncMock()
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.verify_and_alert_usb_drop(printer.id, manager=_FakeManager(), now=NOW, sleep=sleep)
            await usb_storage.verify_and_alert_usb_drop(
                printer.id, manager=_FakeManager(), now=NOW + usb_storage.CLEANUP_COOLDOWN_S + 1, sleep=sleep
            )
            assert notify.await_count == 1  # past the sweep cooldown, still inside the drop window
            after = NOW + usb_storage.USB_DROP_ALERT_COOLDOWN_S + 1
            await usb_storage.verify_and_alert_usb_drop(printer.id, manager=_FakeManager(), now=after, sleep=sleep)
        assert notify.await_count == 2
        assert usb_storage._last_drop_alert_at[printer.id] == after


class TestUploadRegistry:
    """``usb_uploads`` — the leaf registry every upload lane writes and this module reads."""

    async def test_upload_in_flight_registers_for_the_block_and_removes_on_exception(self):
        pid = 99
        with pytest.raises(ValueError):
            async with usb_uploads.upload_in_flight(pid):
                assert usb_uploads.upload_active(pid) is True
                raise ValueError("boom")
        assert usb_uploads.upload_active(pid) is False

    def test_the_registry_left_usb_storage(self):
        """Moved, not re-exported: one owner of "an upload is in flight"."""
        assert not hasattr(usb_storage, "upload_in_flight")
        assert not hasattr(usb_storage, "_uploads_in_flight")
