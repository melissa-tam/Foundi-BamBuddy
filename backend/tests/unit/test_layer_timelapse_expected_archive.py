"""Regression test for #1353: layer timelapse must start for queue/VP-dispatched prints.

Reporter @Andlar94 ran the external-camera flow on an A1 dispatched via the
print queue (so each print landed in the on_print_start "expected archive"
branch). Frames were never captured, no MP4 was produced, yet the post-print
log line said "Stitching layer timelapse for printer 1" — because
`tl_complete()` ran, found no active session, and silently returned None.

Root cause: only the two new-archive code paths in on_print_start
(`fallback_archive` + `archive_print`) called `layer_timelapse.start_session`.
The expected-archive branch — where reprints and queue dispatch land —
updated the existing archive's status to "printing" but never started a
timelapse session.

Fix: start_session is now called in the expected-archive branch too, guarded
by the same `external_camera_enabled and external_camera_url` check that
the other two paths use.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.main import (
    _active_prints,
    _expected_print_creators,
    _expected_print_registered_at,
    _expected_prints,
    _print_ams_mappings,
    register_expected_print,
)


@pytest.fixture(autouse=True)
def _clear_dicts():
    """Clear module-level tracking dicts before and after each test."""
    _expected_prints.clear()
    _expected_print_registered_at.clear()
    _expected_print_creators.clear()
    _print_ams_mappings.clear()
    _active_prints.clear()
    yield
    _expected_prints.clear()
    _expected_print_registered_at.clear()
    _expected_print_creators.clear()
    _print_ams_mappings.clear()
    _active_prints.clear()


def _build_mocks(*, external_camera_enabled: bool, external_camera_url: str | None):
    """Construct the mock matrix needed to drive on_print_start through the
    expected-archive branch. Returns a dict of mock contexts that the test
    enters via contextlib.ExitStack.

    The session.execute mock returns the printer for the first call (printer
    lookup) and the archive row for the second call (expected-archive
    re-fetch). The archive row carries a unique filename so the
    expected-print key lookup succeeds.
    """
    mock_printer = MagicMock()
    mock_printer.id = 1
    mock_printer.auto_archive = True
    mock_printer.external_camera_enabled = external_camera_enabled
    mock_printer.external_camera_url = external_camera_url
    mock_printer.external_camera_type = "snapshot"
    mock_printer.external_camera_snapshot_url = external_camera_url
    mock_printer.name = "TestA1"

    mock_archive = MagicMock()
    mock_archive.id = 42
    mock_archive.filename = "Universal_Spirit_level_Holder.3mf"
    mock_archive.subtask_id = None
    mock_archive.print_time_seconds = None
    mock_archive.created_by_id = None
    mock_archive.printer_id = 1
    mock_archive.print_name = "Universal Spirit Level Holder"
    mock_archive.status = "pending"
    mock_archive.file_path = "/test/archives/fake.3mf"

    return mock_printer, mock_archive


@pytest.mark.asyncio
async def test_expected_archive_path_starts_timelapse_when_external_camera_enabled():
    """Queue/VP-dispatched prints land in the expected-archive branch and must
    start the timelapse session there (the #1353 root cause)."""
    mock_printer, mock_archive = _build_mocks(
        external_camera_enabled=True, external_camera_url="http://camera.local:5000/snapshot.jpg"
    )

    # Register the expected print so the dispatch flow finds an archive_id.
    register_expected_print(1, "Universal_Spirit_level_Holder.3mf", archive_id=42, ams_mapping=[1])

    # on_print_start fires many db.execute() calls (settings lookups,
    # usage tracker, plate detection, etc) before reaching the expected-
    # archive branch. Route on SQL text so each query gets a sensible
    # response regardless of order, rather than queuing N mocks.
    def execute_router(stmt, *args, **kwargs):
        sql = str(stmt).lower()
        if "from printers" in sql or "from printer " in sql:
            return MagicMock(
                scalar_one_or_none=MagicMock(return_value=mock_printer),
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[mock_printer]))),
            )
        if "from print_archives" in sql or "from print_archive" in sql:
            return MagicMock(
                scalar_one_or_none=MagicMock(return_value=mock_archive),
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[mock_archive]))),
            )
        # Settings, spool assignments, anything else — return empty.
        return MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
        )

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=execute_router)
    mock_session.commit = AsyncMock()

    with (
        patch("backend.app.main.async_session") as mock_session_maker,
        patch("backend.app.main.notification_service") as mock_notif,
        patch("backend.app.main.smart_plug_manager") as mock_plug,
        patch("backend.app.main.ws_manager") as mock_ws,
        patch("backend.app.main.printer_manager") as mock_pm,
        patch("backend.app.main.mqtt_relay") as mock_relay,
        patch("backend.app.main._record_energy_start", new_callable=AsyncMock),
        patch("backend.app.main._load_objects_from_archive"),
        patch("backend.app.main._store_spoolman_print_data", new_callable=AsyncMock),
        patch("backend.app.main._send_print_start_notification", new_callable=AsyncMock),
        # The actual subject under test: assert start_session is called.
        patch("backend.app.services.layer_timelapse.start_session") as mock_start_session,
    ):
        mock_session_maker.return_value = mock_session
        mock_notif.on_print_start = AsyncMock()
        mock_plug.on_print_start = AsyncMock()
        mock_ws.send_print_start = AsyncMock()
        mock_ws.send_archive_updated = AsyncMock()
        mock_relay.on_print_start = AsyncMock()
        mock_pm.get_printer = MagicMock(return_value=MagicMock(name="Test", serial_number="TEST123"))

        from backend.app.main import on_print_start

        await on_print_start(
            1,
            {
                "filename": "Universal_Spirit_level_Holder.3mf",
                "subtask_name": "Universal_Spirit_level_Holder",
            },
        )

        mock_start_session.assert_called_once()
        # Verify it was called with the archive_id from the expected-print
        # registration, not a fresh one — that's the contract.
        call_args = mock_start_session.call_args
        assert call_args.args[0] == 1, "printer_id must match"
        assert call_args.args[1] == 42, "archive_id must come from the expected-print registration"
        assert call_args.args[2] == "http://camera.local:5000/snapshot.jpg"
        assert call_args.args[3] == "snapshot"


@pytest.mark.asyncio
async def test_expected_archive_path_skips_timelapse_when_external_camera_disabled():
    """The same guard that the new-archive paths use must hold here: no
    external camera → no timelapse session. Otherwise we'd try to capture
    from a None URL and crash the print-start flow."""
    mock_printer, mock_archive = _build_mocks(external_camera_enabled=False, external_camera_url=None)

    mock_archive.filename = "test.3mf"
    mock_archive.id = 99
    register_expected_print(1, "test.3mf", archive_id=99, ams_mapping=None)

    def execute_router(stmt, *args, **kwargs):
        sql = str(stmt).lower()
        if "from printers" in sql or "from printer " in sql:
            return MagicMock(
                scalar_one_or_none=MagicMock(return_value=mock_printer),
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[mock_printer]))),
            )
        if "from print_archives" in sql or "from print_archive" in sql:
            return MagicMock(
                scalar_one_or_none=MagicMock(return_value=mock_archive),
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[mock_archive]))),
            )
        return MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
        )

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=execute_router)
    mock_session.commit = AsyncMock()

    with (
        patch("backend.app.main.async_session") as mock_session_maker,
        patch("backend.app.main.notification_service") as mock_notif,
        patch("backend.app.main.smart_plug_manager") as mock_plug,
        patch("backend.app.main.ws_manager") as mock_ws,
        patch("backend.app.main.printer_manager") as mock_pm,
        patch("backend.app.main.mqtt_relay") as mock_relay,
        patch("backend.app.main._record_energy_start", new_callable=AsyncMock),
        patch("backend.app.main._load_objects_from_archive"),
        patch("backend.app.main._store_spoolman_print_data", new_callable=AsyncMock),
        patch("backend.app.main._send_print_start_notification", new_callable=AsyncMock),
        patch("backend.app.services.layer_timelapse.start_session") as mock_start_session,
    ):
        mock_session_maker.return_value = mock_session
        mock_notif.on_print_start = AsyncMock()
        mock_plug.on_print_start = AsyncMock()
        mock_ws.send_print_start = AsyncMock()
        mock_ws.send_archive_updated = AsyncMock()
        mock_relay.on_print_start = AsyncMock()
        mock_pm.get_printer = MagicMock(return_value=MagicMock(name="Test", serial_number="TEST123"))

        from backend.app.main import on_print_start

        await on_print_start(1, {"filename": "test.3mf", "subtask_name": "test"})

        mock_start_session.assert_not_called()
