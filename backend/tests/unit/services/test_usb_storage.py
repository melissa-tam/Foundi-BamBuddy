"""USB storage-low auto-cleanup tests.

``usb_storage.on_storage_low`` reacts to a new "USB full" HMS code by cleaning the
printer's USB drive (camera recordings first, then oldest UNUSED root print files)
and firing the dedicated ``on_storage_low`` notification with the outcome. It NEVER
raises — an unreachable FTPS printer surfaces as the failure notification.

FTPS is mocked at the ``usb_storage`` module boundary (``list_files_async`` /
``delete_file_async`` / ``get_storage_info_async`` / ``_ftp_reachable``); the live
printer state is injected via a fake manager; the DB session is the in-memory test
engine (patched onto ``core.database.async_session``). Notifications are asserted by
patching the ``notification_service`` singleton.
"""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import usb_storage
from backend.app.services.bambu_ftp import DeleteResult
from backend.app.services.notification_service import notification_service

pytestmark = pytest.mark.asyncio

NOW = 1_000_000.0
_FULL = {"0500010000030004"}
_GB = 1024**3


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #
class _FakeState:
    def __init__(self, state="IDLE", gcode_file=None, subtask_name=None, sdcard=False):
        self.state = state
        self.gcode_file = gcode_file
        self.subtask_name = subtask_name
        self.sdcard = sdcard


class _FakeClient:
    def __init__(self, state: _FakeState):
        self.state = state


class _FakeManager:
    """Injectable stand-in for printer_manager exposing get_client()."""

    def __init__(self, client: _FakeClient | None = None):
        self._client = client

    def get_client(self, printer_id: int):
        return self._client


def _mtime(age_s: float) -> datetime:
    """A file mtime ``age_s`` seconds before NOW."""
    return datetime.fromtimestamp(NOW - age_s)


def _rec(name: str, *, path: str, size: int, age_s: float | None = 3 * 3600, is_dir: bool = False) -> dict:
    entry: dict = {"name": name, "is_directory": is_dir, "size": size, "path": path}
    if age_s is not None:
        entry["mtime"] = _mtime(age_s)
    return entry


@pytest.fixture(autouse=True)
def _clean_state():
    usb_storage._reset_state()
    yield
    usb_storage._reset_state()


@pytest.fixture
def session_maker(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


def _patch_session(session_maker):
    return patch("backend.app.core.database.async_session", session_maker)


def _list_from(mapping: dict[str, list[dict]]):
    async def _list(ip, access_code, path="/", printer_model=None, **kw):
        return list(mapping.get(path, []))

    return AsyncMock(side_effect=_list)


def _storage_returning(value):
    async def _get(ip, access_code, printer_model=None, **kw):
        return value

    return AsyncMock(side_effect=_get)


def _delete_recorder(deleted: list[str], result: DeleteResult = DeleteResult.DELETED):
    async def _del(ip, access_code, path, printer_model=None, **kw):
        deleted.append(path)
        return result

    return AsyncMock(side_effect=_del)


# --------------------------------------------------------------------------- #
# Guards: disabled / cooldown / printing
# --------------------------------------------------------------------------- #
class TestGuards:
    async def test_disabled_setting_is_noop(self, db_session, session_maker, printer_factory):
        from backend.app.api.routes.settings import set_setting

        printer = await printer_factory(model="H2S")
        await set_setting(db_session, "farm_usb_auto_cleanup", "false")
        await db_session.commit()

        list_mock = _list_from({})
        del_mock = _delete_recorder([])
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "_ftp_reachable", new=AsyncMock(return_value=True)) as reach,
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(usb_storage, "delete_file_async", new=del_mock),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_FakeManager(), now=NOW)

        reach.assert_not_awaited()
        list_mock.assert_not_awaited()
        del_mock.assert_not_awaited()
        notify.assert_not_awaited()

    async def test_cooldown_suppresses_second_run(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        usb_storage._last_cleanup_at[printer.id] = NOW  # within CLEANUP_COOLDOWN_S

        list_mock = _list_from({})
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "_ftp_reachable", new=AsyncMock(return_value=True)) as reach,
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(usb_storage, "delete_file_async", new=_delete_recorder([])),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_FakeManager(), now=NOW + 60)

        reach.assert_not_awaited()
        list_mock.assert_not_awaited()
        notify.assert_not_awaited()

    async def test_printing_printer_is_skipped(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        mgr = _FakeManager(_FakeClient(_FakeState(state="RUNNING")))

        list_mock = _list_from({})
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "_ftp_reachable", new=AsyncMock(return_value=True)) as reach,
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(usb_storage, "delete_file_async", new=_delete_recorder([])),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.on_storage_low(printer.id, _FULL, manager=mgr, now=NOW)

        reach.assert_not_awaited()
        list_mock.assert_not_awaited()
        notify.assert_not_awaited()
        # The printing guard must NOT burn the cooldown — a later idle tick may still clean.
        assert printer.id not in usb_storage._last_cleanup_at
        # ...and it registers the deferral so the main.py hook can retry post-print.
        assert usb_storage.has_deferred(printer.id)


# --------------------------------------------------------------------------- #
# Deferred retry (printing guard → retry once the print ends)
# --------------------------------------------------------------------------- #
class TestDeferredRetry:
    async def test_retry_after_print_finishes_proceeds_and_clears_deferral(
        self, db_session, session_maker, printer_factory
    ):
        printer = await printer_factory(model="H2S")
        listing = {
            "/ipcam": [_rec("old.jpg", path="/ipcam/old.jpg", size=1_000_000, age_s=2 * 3600)],
            "/ipcam/thumbnail": [],
            "/timelapse": [],
        }
        deleted: list[str] = []
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "_ftp_reachable", new=AsyncMock(return_value=True)),
            patch.object(usb_storage, "list_files_async", new=_list_from(listing)),
            patch.object(usb_storage, "delete_file_async", new=_delete_recorder(deleted)),
            patch.object(usb_storage, "get_storage_info_async", new=_storage_returning(None)),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            # 1st trigger while printing → deferred, nothing touched.
            printing = _FakeManager(_FakeClient(_FakeState(state="RUNNING")))
            await usb_storage.on_storage_low(printer.id, _FULL, manager=printing, now=NOW)
            assert usb_storage.has_deferred(printer.id)
            assert deleted == []
            notify.assert_not_awaited()

            # 2nd trigger once idle → proceeds (no deferral left, cooldown untouched
            # by the deferral), cleans, notifies, stamps cooldown, clears the flag.
            idle = _FakeManager(_FakeClient(_FakeState(state="IDLE")))
            await usb_storage.on_storage_low(printer.id, _FULL, manager=idle, now=NOW + 120)

        assert deleted == ["/ipcam/old.jpg"]
        # Success → silent (errors-only). Deferral cleared, cooldown stamped.
        notify.assert_not_awaited()
        assert not usb_storage.has_deferred(printer.id)
        assert usb_storage._last_cleanup_at[printer.id] == NOW + 120

    async def test_still_printing_retry_re_defers(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        printing = _FakeManager(_FakeClient(_FakeState(state="PREPARE")))
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "_ftp_reachable", new=AsyncMock(return_value=True)),
            patch.object(usb_storage, "list_files_async", new=_list_from({})),
            patch.object(usb_storage, "delete_file_async", new=_delete_recorder([])),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock),
        ):
            await usb_storage.on_storage_low(printer.id, _FULL, manager=printing, now=NOW)
            await usb_storage.on_storage_low(printer.id, _FULL, manager=printing, now=NOW + 60)
        # Re-deferred, still no cooldown burned.
        assert usb_storage.has_deferred(printer.id)
        assert printer.id not in usb_storage._last_cleanup_at

    async def test_disabled_setting_consumes_deferral(self, db_session, session_maker, printer_factory):
        """A disabled-setting retry clears the flag so the main.py hook can't spin
        a DB-opening task on every subsequent status tick."""
        from backend.app.api.routes.settings import set_setting

        printer = await printer_factory(model="H2S")
        await set_setting(db_session, "farm_usb_auto_cleanup", "false")
        await db_session.commit()
        usb_storage._deferred_printers.add(printer.id)

        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "_ftp_reachable", new=AsyncMock(return_value=True)),
            patch.object(usb_storage, "list_files_async", new=_list_from({})),
            patch.object(usb_storage, "delete_file_async", new=_delete_recorder([])),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock),
        ):
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_FakeManager(), now=NOW)

        assert not usb_storage.has_deferred(printer.id)

    async def test_should_retry_deferred_predicate(self):
        pid = 7

        # No deferral registered → never retry.
        assert usb_storage.should_retry_deferred(pid, "IDLE") is False

        usb_storage._deferred_printers.add(pid)
        # Deferred + idle → retry, INDEPENDENT of HMS presence: the firmware drops
        # the storage code from hms[] when it unmounts a full drive, so a
        # presence-gated retry would wait forever (the 009/printer-7 silent stall).
        assert usb_storage.should_retry_deferred(pid, "IDLE") is True
        assert usb_storage.should_retry_deferred(pid, None) is True
        assert usb_storage.should_retry_deferred(pid, "FINISH") is True
        # Still printing → no retry (both active states, case-insensitive).
        assert usb_storage.should_retry_deferred(pid, "RUNNING") is False
        assert usb_storage.should_retry_deferred(pid, "prepare") is False
        # Predicate is pure — it must not consume the deferral.
        assert usb_storage.has_deferred(pid)

    async def test_deferred_retry_on_unmounted_drive_fires_failure_notification(
        self, db_session, session_maker, printer_factory
    ):
        """The 009/printer-7 fix end-to-end: a cleanup deferred while printing, then
        retried once idle against a now-UNMOUNTED drive (hms[] cleared), must surface
        the FTPS-unreachable FAILURE notification instead of silence."""
        printer = await printer_factory(model="H2S")
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "_ftp_reachable", new=AsyncMock(return_value=False)),
            patch.object(usb_storage, "list_files_async", new=_list_from({})),
            patch.object(usb_storage, "delete_file_async", new=_delete_recorder([])),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            # Deferred while printing.
            printing = _FakeManager(_FakeClient(_FakeState(state="RUNNING")))
            await usb_storage.on_storage_low(printer.id, _FULL, manager=printing, now=NOW)
            assert usb_storage.has_deferred(printer.id)
            notify.assert_not_awaited()

            # Retried once idle — the drive is gone → failure notification.
            idle = _FakeManager(_FakeClient(_FakeState(state="IDLE")))
            await usb_storage.on_storage_low(printer.id, _FULL, manager=idle, now=NOW + 120)

        notify.assert_awaited_once()
        kwargs = notify.await_args.kwargs
        assert kwargs["success"] is False
        assert kwargs["reason"]  # a human reason is carried
        # Deferral consumed — no endless per-tick retries.
        assert not usb_storage.has_deferred(printer.id)


# --------------------------------------------------------------------------- #
# Single-flight, USB-drop edge, and the post-print recordings drain
# --------------------------------------------------------------------------- #
class TestSingleFlight:
    async def test_inflight_collapses_concurrent_triggers(self, db_session, session_maker, printer_factory):
        """A pass already running (printer in _inflight) makes a second concurrent
        trigger a no-op — no FTP work, no notification."""
        printer = await printer_factory(model="H2S")
        usb_storage._inflight.add(printer.id)

        list_mock = _list_from({})
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "_ftp_reachable", new=AsyncMock(return_value=True)) as reach,
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(usb_storage, "delete_file_async", new=_delete_recorder([])),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_FakeManager(), now=NOW)
            await usb_storage.drain_recordings_if_idle(printer.id, manager=_FakeManager(), now=NOW)

        reach.assert_not_awaited()
        list_mock.assert_not_awaited()
        notify.assert_not_awaited()

    async def test_inflight_released_after_run(self, db_session, session_maker, printer_factory):
        """A completed pass releases the single-flight slot (discarded in finally)."""
        printer = await printer_factory(model="H2S")
        listing = {"/ipcam": [], "/ipcam/thumbnail": [], "/timelapse": []}
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "_ftp_reachable", new=AsyncMock(return_value=True)),
            patch.object(usb_storage, "list_files_async", new=_list_from(listing)),
            patch.object(usb_storage, "delete_file_async", new=_delete_recorder([])),
            patch.object(usb_storage, "get_storage_info_async", new=_storage_returning(None)),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock),
        ):
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_FakeManager(), now=NOW)

        assert printer.id not in usb_storage._inflight


class TestSdcardDrop:
    async def test_never_fires_from_startup_default(self):
        # Never-reported → default False observed: no drop, and we don't even record
        # it (so a later True is a clean first observation).
        assert usb_storage.record_sdcard_and_detect_drop(1, False) is False
        assert 1 not in usb_storage._last_sdcard

    async def test_fires_once_on_genuine_transition(self):
        # Drive observed present, then drops → fires exactly once.
        assert usb_storage.record_sdcard_and_detect_drop(2, True) is False
        assert usb_storage.record_sdcard_and_detect_drop(2, False) is True
        # Still absent on the next tick → does NOT re-fire.
        assert usb_storage.record_sdcard_and_detect_drop(2, False) is False

    async def test_rearms_after_remount(self):
        assert usb_storage.record_sdcard_and_detect_drop(3, True) is False
        assert usb_storage.record_sdcard_and_detect_drop(3, False) is True
        # Remount, then a second drop fires again.
        assert usb_storage.record_sdcard_and_detect_drop(3, True) is False
        assert usb_storage.record_sdcard_and_detect_drop(3, False) is True


class TestVerifyAndAlertUsbDrop:
    """The verify-then-alert flow behind main.py's ``sdcard`` True→False edge.

    ``record_sdcard_and_detect_drop`` (above) still owns the pure edge; main.py now
    spawns ``verify_and_alert_usb_drop`` which does NOT trust the edge blindly:
    upload-in-flight suppression, an FTPS re-probe that filters ~1 s dispatch blips,
    live-state cancellation, and a per-printer 6 h alert cooldown. Only a
    probe-confirmed drop fires the ``attempted=False`` notification. FTPS/list is
    mocked at the module boundary; the alert sleep is injected so the suite stays
    fast (no real 15 s wait)."""

    async def test_edge_during_upload_is_suppressed(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        usb_storage._uploads_in_flight.add(printer.id)
        list_mock = _list_from({"/": []})
        sleep = AsyncMock()
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.verify_and_alert_usb_drop(printer.id, manager=_FakeManager(), now=NOW, sleep=sleep)
        # Suppressed before any probe / wait / notification.
        notify.assert_not_awaited()
        list_mock.assert_not_awaited()
        sleep.assert_not_awaited()

    async def test_probe_sees_usb_present_suppressed(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        # A non-empty root on the FIRST probe → USB present → suppress, no re-probe.
        list_mock = _list_from({"/": [_rec("a.3mf", path="/a.3mf", size=_GB)]})
        sleep = AsyncMock()
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.verify_and_alert_usb_drop(printer.id, manager=_FakeManager(), now=NOW, sleep=sleep)
        notify.assert_not_awaited()
        sleep.assert_not_awaited()  # present on first probe → never waits
        assert printer.id not in usb_storage._last_drop_alert_at

    async def test_confirmed_drop_alerts_once(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        # Empty root on BOTH probes + no live sdcard → confirmed drop.
        list_mock = _list_from({"/": []})
        sleep = AsyncMock()
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.verify_and_alert_usb_drop(printer.id, manager=_FakeManager(), now=NOW, sleep=sleep)
        sleep.assert_awaited_once()  # one confirming re-probe delay
        notify.assert_awaited_once()
        kwargs = notify.await_args.kwargs
        assert kwargs["attempted"] is False
        assert kwargs["success"] is False
        assert kwargs["reason"] == usb_storage.USB_DROP_REASON
        assert usb_storage._last_drop_alert_at[printer.id] == NOW

    async def test_upload_starting_after_first_probe_is_suppressed(self, db_session, session_maker, printer_factory):
        """Race: the edge fires just BEFORE a dispatch upload begins, then both FTPS
        probes fail on session contention against that now-running upload. The
        post-re-probe upload-in-flight re-check suppresses the false confirmed drop."""
        printer = await printer_factory(model="H2S")
        list_mock = _list_from({"/": []})  # both probes see an empty/contended root

        async def _sleep(_seconds):
            # The dispatch upload begins during the confirmation wait.
            usb_storage._uploads_in_flight.add(printer.id)

        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.verify_and_alert_usb_drop(printer.id, manager=_FakeManager(), now=NOW, sleep=_sleep)
        notify.assert_not_awaited()  # re-check caught the now-in-flight upload
        assert printer.id not in usb_storage._last_drop_alert_at

    async def test_probe_exception_treated_as_absent_and_alerts(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        # list_files_async raising (port 990 down / login rejected) reads as "absent".
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
        # Both probes empty, but the printer reports the USB back (live sdcard=True)
        # before the alert → cancelled, no notification.
        list_mock = _list_from({"/": []})
        mgr = _FakeManager(_FakeClient(_FakeState(state="IDLE", sdcard=True)))
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
        printer = await printer_factory(model="H2S")
        list_mock = _list_from({"/": []})
        sleep = AsyncMock()
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            # 1st confirmed drop → alert.
            await usb_storage.verify_and_alert_usb_drop(printer.id, manager=_FakeManager(), now=NOW, sleep=sleep)
            # 2nd within the 6 h cooldown → suppressed (a flapping stick can't spam).
            await usb_storage.verify_and_alert_usb_drop(printer.id, manager=_FakeManager(), now=NOW + 60, sleep=sleep)
            # After the cooldown → fires again.
            after = NOW + usb_storage.CLEANUP_COOLDOWN_S + 1
            await usb_storage.verify_and_alert_usb_drop(printer.id, manager=_FakeManager(), now=after, sleep=sleep)
        assert notify.await_count == 2
        assert usb_storage._last_drop_alert_at[printer.id] == after

    async def test_upload_in_flight_removes_id_on_exception(self):
        pid = 99
        with pytest.raises(ValueError):
            async with usb_storage.upload_in_flight(pid):
                assert pid in usb_storage._uploads_in_flight
                raise ValueError("boom")
        # Always removed on exit, even when the wrapped block raised.
        assert pid not in usb_storage._uploads_in_flight


class TestPostPrintDrain:
    async def test_drain_deletes_recordings_and_never_notifies(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        listing = {
            "/ipcam": [_rec("old.jpg", path="/ipcam/old.jpg", size=_GB, age_s=4 * 3600)],
            "/ipcam/thumbnail": [],
            "/timelapse": [],
            # A stale root print file that a full cleanup WOULD delete in stage 2 —
            # the drain must never touch it (recordings-only).
            "/": [_rec("stale.3mf", path="/stale.3mf", size=5 * _GB, age_s=30 * 3600)],
        }
        deleted: list[str] = []
        info_mock = _storage_returning({"free_bytes": 0})  # would trigger stage 2 if consulted
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "_ftp_reachable", new=AsyncMock(return_value=True)),
            patch.object(usb_storage, "list_files_async", new=_list_from(listing)),
            patch.object(usb_storage, "delete_file_async", new=_delete_recorder(deleted)),
            patch.object(usb_storage, "get_storage_info_async", new=info_mock),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.drain_recordings_if_idle(printer.id, manager=_FakeManager(), now=NOW)

        assert deleted == ["/ipcam/old.jpg"]  # recordings only; print file untouched
        info_mock.assert_not_awaited()  # stage 2 never consulted
        notify.assert_not_awaited()  # opportunistic — never notifies
        assert usb_storage._last_cleanup_at[printer.id] == NOW  # shares the cooldown

    async def test_drain_respects_gates(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        list_mock = _list_from({})

        # (a) disabled setting → no-op
        from backend.app.api.routes.settings import set_setting

        await set_setting(db_session, "farm_usb_auto_cleanup", "false")
        await db_session.commit()
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "_ftp_reachable", new=AsyncMock(return_value=True)) as reach,
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(usb_storage, "delete_file_async", new=_delete_recorder([])),
        ):
            await usb_storage.drain_recordings_if_idle(printer.id, manager=_FakeManager(), now=NOW)
        reach.assert_not_awaited()

        await set_setting(db_session, "farm_usb_auto_cleanup", "true")
        await db_session.commit()

        # (b) printing → no-op, and (unlike the HMS path) records NO deferral.
        printing = _FakeManager(_FakeClient(_FakeState(state="RUNNING")))
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "_ftp_reachable", new=AsyncMock(return_value=True)) as reach,
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(usb_storage, "delete_file_async", new=_delete_recorder([])),
        ):
            await usb_storage.drain_recordings_if_idle(printer.id, manager=printing, now=NOW)
        reach.assert_not_awaited()
        assert not usb_storage.has_deferred(printer.id)

        # (c) within cooldown → no-op
        usb_storage._last_cleanup_at[printer.id] = NOW
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "_ftp_reachable", new=AsyncMock(return_value=True)) as reach,
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(usb_storage, "delete_file_async", new=_delete_recorder([])),
        ):
            await usb_storage.drain_recordings_if_idle(printer.id, manager=_FakeManager(), now=NOW + 60)
        reach.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Stage 1: recordings
# --------------------------------------------------------------------------- #
class TestStageOneRecordings:
    async def test_deletes_only_old_recordings(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        listing = {
            "/ipcam": [
                _rec("old.jpg", path="/ipcam/old.jpg", size=1_000_000, age_s=2 * 3600),  # old → delete
                _rec("fresh.jpg", path="/ipcam/fresh.jpg", size=1_000_000, age_s=60),  # too new → keep
                _rec("nostamp.jpg", path="/ipcam/nostamp.jpg", size=1_000_000, age_s=None),  # no mtime → keep
                _rec("subdir", path="/ipcam/subdir", size=0, age_s=2 * 3600, is_dir=True),  # dir → keep
            ],
            "/ipcam/thumbnail": [
                _rec("t_old.jpg", path="/ipcam/thumbnail/t_old.jpg", size=500_000, age_s=5 * 3600),
            ],
            "/timelapse": [],
        }
        deleted: list[str] = []
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "_ftp_reachable", new=AsyncMock(return_value=True)),
            patch.object(usb_storage, "list_files_async", new=_list_from(listing)),
            patch.object(usb_storage, "delete_file_async", new=_delete_recorder(deleted)),
            # Free space unknown → stage 2 never entered.
            patch.object(usb_storage, "get_storage_info_async", new=_storage_returning(None)),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_FakeManager(), now=NOW)

        assert deleted == ["/ipcam/old.jpg", "/ipcam/thumbnail/t_old.jpg"]
        # Errors-only: a successful cleanup is silent (INFO audit log only).
        notify.assert_not_awaited()

    async def test_stage2_not_entered_when_free_unknown(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        list_mock = _list_from(
            {
                "/ipcam": [],
                "/ipcam/thumbnail": [],
                "/timelapse": [],
                "/": [_rec("stale.3mf", path="/stale.3mf", size=5 * _GB, age_s=10 * 3600)],
            }
        )
        deleted: list[str] = []
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "_ftp_reachable", new=AsyncMock(return_value=True)),
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(usb_storage, "delete_file_async", new=_delete_recorder(deleted)),
            patch.object(usb_storage, "get_storage_info_async", new=_storage_returning(None)),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock),
        ):
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_FakeManager(), now=NOW)

        # Root was never listed and nothing deleted — stage 2 is gated on known free space.
        assert deleted == []
        listed_paths = {c.kwargs.get("path", c.args[2] if len(c.args) > 2 else "/") for c in list_mock.await_args_list}
        assert "/" not in listed_paths


# --------------------------------------------------------------------------- #
# Stage 2: oldest unused print files
# --------------------------------------------------------------------------- #
class TestStageTwoPrintFiles:
    async def _run(self, db_session, session_maker, printer, listing, free_bytes, mgr=None):
        deleted: list[str] = []
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "_ftp_reachable", new=AsyncMock(return_value=True)),
            patch.object(usb_storage, "list_files_async", new=_list_from(listing)),
            patch.object(usb_storage, "delete_file_async", new=_delete_recorder(deleted)),
            patch.object(usb_storage, "get_storage_info_async", new=_storage_returning({"free_bytes": free_bytes})),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.on_storage_low(printer.id, _FULL, manager=mgr or _FakeManager(), now=NOW)
        return deleted, notify

    async def test_oldest_first_and_stops_at_target(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        # Three deletable print files, each 1 GB. Free starts at 0.5 GB (< 2 GB target).
        # Need +1.5 GB to reach target → exactly 2 files, oldest first.
        listing = {
            "/ipcam": [],
            "/ipcam/thumbnail": [],
            "/timelapse": [],
            "/": [
                _rec("newest.3mf", path="/newest.3mf", size=_GB, age_s=2 * 3600),
                _rec("oldest.3mf", path="/oldest.3mf", size=_GB, age_s=10 * 3600),
                _rec("middle.3mf", path="/middle.3mf", size=_GB, age_s=5 * 3600),
            ],
        }
        deleted, notify = await self._run(db_session, session_maker, printer, listing, free_bytes=_GB // 2)

        assert deleted == ["/oldest.3mf", "/middle.3mf"]  # oldest-first, stop once target met
        # Success → silent (errors-only).
        notify.assert_not_awaited()

    async def test_excludes_in_use_files(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")

        # Queue item (pending) whose archive file is the OLDEST root file → must be kept.
        archive = PrintArchive(
            printer_id=printer.id,
            filename="queued_part.gcode.3mf",
            print_name="Queued",
            file_path="archives/q/queued_part.gcode.3mf",
            file_size=123,
            status="queued",
        )
        db_session.add(archive)
        await db_session.commit()
        await db_session.refresh(archive)
        db_session.add(
            PrintQueueItem(printer_id=printer.id, status="pending", archive_id=archive.id, plate_id=1, position=1)
        )
        await db_session.commit()

        # Live printer has a different file loaded → also excluded.
        mgr = _FakeManager(_FakeClient(_FakeState(state="IDLE", gcode_file="loaded.3mf")))

        listing = {
            "/ipcam": [],
            "/ipcam/thumbnail": [],
            "/timelapse": [],
            "/": [
                _rec("queued_part.3mf", path="/queued_part.3mf", size=5 * _GB, age_s=20 * 3600),  # in use (queue)
                _rec("loaded.3mf", path="/loaded.3mf", size=5 * _GB, age_s=18 * 3600),  # in use (live)
                _rec("junk.3mf", path="/junk.3mf", size=_GB, age_s=6 * 3600),  # deletable
            ],
        }
        deleted, notify = await self._run(db_session, session_maker, printer, listing, free_bytes=_GB // 2, mgr=mgr)

        assert deleted == ["/junk.3mf"]  # neither in-use file touched

    async def test_recordings_deleted_before_print_files(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        listing = {
            "/ipcam": [_rec("rec.jpg", path="/ipcam/rec.jpg", size=_GB, age_s=4 * 3600)],
            "/ipcam/thumbnail": [],
            "/timelapse": [],
            "/": [_rec("stale.3mf", path="/stale.3mf", size=5 * _GB, age_s=30 * 3600)],
        }
        deleted, _ = await self._run(db_session, session_maker, printer, listing, free_bytes=0)

        assert deleted[0] == "/ipcam/rec.jpg"  # stage 1 (recordings) fully runs before stage 2
        assert "/stale.3mf" in deleted
        assert deleted.index("/ipcam/rec.jpg") < deleted.index("/stale.3mf")


# --------------------------------------------------------------------------- #
# Failure path
# --------------------------------------------------------------------------- #
class TestFailurePath:
    async def test_unreachable_ftps_fires_failure_notification(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        list_mock = _list_from({})
        del_mock = _delete_recorder([])
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "_ftp_reachable", new=AsyncMock(return_value=False)),
            patch.object(usb_storage, "list_files_async", new=list_mock),
            patch.object(usb_storage, "delete_file_async", new=del_mock),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            # Must NOT raise even though FTPS is down.
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_FakeManager(), now=NOW)

        list_mock.assert_not_awaited()
        del_mock.assert_not_awaited()
        notify.assert_awaited_once()
        kwargs = notify.await_args.kwargs
        assert kwargs["success"] is False
        assert kwargs["reason"]  # a human reason is carried

    async def test_nothing_to_clean_reports_unsuccessful(self, db_session, session_maker, printer_factory):
        printer = await printer_factory(model="H2S")
        listing = {"/ipcam": [], "/ipcam/thumbnail": [], "/timelapse": []}
        with (
            _patch_session(session_maker),
            patch.object(usb_storage, "_ftp_reachable", new=AsyncMock(return_value=True)),
            patch.object(usb_storage, "list_files_async", new=_list_from(listing)),
            patch.object(usb_storage, "delete_file_async", new=_delete_recorder([])),
            patch.object(usb_storage, "get_storage_info_async", new=_storage_returning({"free_bytes": _GB // 2})),
            patch.object(notification_service, "on_storage_low", new_callable=AsyncMock) as notify,
        ):
            await usb_storage.on_storage_low(printer.id, _FULL, manager=_FakeManager(), now=NOW)

        notify.assert_awaited_once()
        kwargs = notify.await_args.kwargs
        assert kwargs["success"] is False
        assert kwargs["files_deleted"] == 0
