"""Unit tests for the filament usage tracker.

Tests 3MF-primary tracking (Path 1) and AMS remain% delta fallback
(Path 2) for spools not covered by 3MF data.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services.usage_tracker import (
    PrintSession,
    _active_sessions,
    _archive_colors_from_spools,
    _assign_segments_to_slots,
    _spool_color_to_hex,
    _track_from_3mf,
    on_print_complete,
    on_print_start,
)


def _make_spool(*, id=1, label_weight=1000, weight_used=0, tag_uid=None, tray_uuid=None, rgba=None):
    """Create a mock Spool object."""
    spool = MagicMock()
    spool.id = id
    spool.label_weight = label_weight
    spool.weight_used = weight_used
    spool.tag_uid = tag_uid
    spool.tray_uuid = tray_uuid
    spool.last_used = None
    spool.cost_per_kg = None
    spool.material = "PLA"
    spool.rgba = rgba
    return spool


def _make_assignment(*, spool_id=1, printer_id=1, ams_id=0, tray_id=0, created_at=None):
    """Create a mock SpoolAssignment object."""
    assignment = MagicMock()
    assignment.spool_id = spool_id
    assignment.printer_id = printer_id
    assignment.ams_id = ams_id
    assignment.tray_id = tray_id
    assignment.created_at = created_at or datetime.now(timezone.utc)
    return assignment


def _make_printer_state(ams_data, progress=0, layer_num=0, tray_now=255):
    """Create a mock printer state with AMS data."""
    state = MagicMock()
    state.raw_data = {"ams": ams_data}
    state.progress = progress
    state.layer_num = layer_num
    state.tray_now = tray_now
    return state


def _make_printer_manager(state=None):
    """Create a mock printer manager."""
    pm = MagicMock()
    pm.get_status.return_value = state
    return pm


class TestOnPrintStart:
    """Tests for on_print_start — capturing AMS remain%."""

    @pytest.fixture(autouse=True)
    def _clear_sessions(self):
        _active_sessions.clear()
        yield
        _active_sessions.clear()

    @pytest.mark.asyncio
    async def test_creates_session_with_valid_remain(self):
        """Session created with remain% data for trays reporting 0-100."""
        ams_data = [{"id": 0, "tray": [{"id": 0, "remain": 80}]}]
        pm = _make_printer_manager(_make_printer_state(ams_data))

        await on_print_start(1, {"subtask_name": "test_print"}, pm)

        assert 1 in _active_sessions
        session = _active_sessions[1]
        assert session.print_name == "test_print"
        assert session.tray_remain_start == {(0, 0): 80}

    @pytest.mark.asyncio
    async def test_creates_session_even_without_valid_remain(self):
        """Session still created when remain=-1 (for 3MF fallback path)."""
        ams_data = [{"id": 0, "tray": [{"id": 0, "remain": -1}]}]
        pm = _make_printer_manager(_make_printer_state(ams_data))

        await on_print_start(1, {"subtask_name": "test_print"}, pm)

        assert 1 in _active_sessions
        session = _active_sessions[1]
        assert session.tray_remain_start == {}  # Empty, no valid remain

    @pytest.mark.asyncio
    async def test_skips_without_ams_data(self):
        """No session created when no AMS data available."""
        state = MagicMock()
        state.raw_data = {"ams": []}
        pm = _make_printer_manager(state)

        await on_print_start(1, {"subtask_name": "test"}, pm)

        assert 1 not in _active_sessions


class TestOnPrintCompleteAMSDelta:
    """Tests for Path 1: AMS remain% delta tracking."""

    @pytest.fixture(autouse=True)
    def _clear_sessions(self):
        _active_sessions.clear()
        yield
        _active_sessions.clear()

    @pytest.fixture(autouse=True)
    def _mock_get_setting(self):
        with patch(
            "backend.app.api.routes.settings.get_setting",
            new_callable=AsyncMock,
            return_value=None,
        ):
            yield

    @pytest.mark.asyncio
    async def test_computes_delta_and_updates_spool(self):
        """Spool weight_used updated by remain% delta * label_weight."""
        # Set up session with start remain = 80%
        _active_sessions[1] = PrintSession(
            printer_id=1,
            print_name="test",
            started_at=datetime.now(timezone.utc),
            tray_remain_start={(0, 0): 80},
        )

        # Current remain = 70% → 10% consumed → 100g on 1000g spool
        ams_data = [{"id": 0, "tray": [{"id": 0, "remain": 70}]}]
        pm = _make_printer_manager(_make_printer_state(ams_data))

        spool = _make_spool(label_weight=1000, weight_used=50)
        assignment = _make_assignment()

        db = AsyncMock()
        # First 2 executes → _find_3mf_by_filename (library + archive search, uses scalars().all()),
        # then assignment, then spool for the AMS fallback path
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(),  # _find_3mf_by_filename: library search
                MagicMock(),  # _find_3mf_by_filename: archive search
                MagicMock(scalar_one_or_none=MagicMock(return_value=assignment)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),
            ]
        )

        results = await on_print_complete(1, {"status": "completed"}, pm, db)

        assert len(results) == 1
        assert results[0]["weight_used"] == 100.0
        assert results[0]["percent_used"] == 10
        # weight_used should be old (50) + delta (100)
        assert spool.weight_used == 150.0
        db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_negative_delta(self):
        """No tracking when remain increased (spool refilled)."""
        _active_sessions[1] = PrintSession(
            printer_id=1,
            print_name="test",
            started_at=datetime.now(timezone.utc),
            tray_remain_start={(0, 0): 50},
        )

        # Remain went UP: 50 → 80 (refilled)
        ams_data = [{"id": 0, "tray": [{"id": 0, "remain": 80}]}]
        pm = _make_printer_manager(_make_printer_state(ams_data))
        db = AsyncMock()

        results = await on_print_complete(1, {"status": "completed"}, pm, db)

        assert results == []
        db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_session_falls_through_to_3mf(self):
        """When no session exists, AMS delta path skipped (3MF may still run)."""
        pm = _make_printer_manager()
        db = AsyncMock()

        results = await on_print_complete(1, {"status": "completed"}, pm, db)

        assert results == []

    @pytest.mark.asyncio
    async def test_skips_fallback_for_trays_outside_print_mapping(self):
        """#1269: swapping a spool in an UNUSED slot mid-print must NOT charge the old spool.

        Reproduces maugsburger's report: single-color print on AMS0-T3
        (ams_mapping=[3]). User swaps spools in T1 and T2 during the print —
        those slots report remain=0 at completion (new spool with no tag).
        The fallback must skip T1 and T2 because they were never in the
        print's tray mapping or runtime tray_change_log.
        """
        _active_sessions[1] = PrintSession(
            printer_id=1,
            print_name="splitter",
            started_at=datetime.now(timezone.utc),
            tray_remain_start={(0, 1): 100, (0, 2): 17, (0, 3): 100},
            tray_now_at_start=3,
            ams_mapping=[3],
        )

        # User swapped T1 and T2 mid-print → both report remain=0 now.
        # T3 was actually used but it's also at 0 now. Without the fix the
        # fallback would charge the originally-assigned spools at T1 and T2.
        ams_data = [
            {
                "id": 0,
                "tray": [
                    {"id": 1, "remain": 0},
                    {"id": 2, "remain": 0},
                    {"id": 3, "remain": 0},
                ],
            }
        ]
        state = _make_printer_state(ams_data, tray_now=3)
        state.tray_change_log = [(3, 0)]  # only T3 was loaded during the print
        pm = _make_printer_manager(state)

        # Only T3 should reach the spool lookup; T1 and T2 must be filtered
        # out before any DB query is issued for them.
        t3_spool = _make_spool(id=8, label_weight=1000, weight_used=0)
        t3_assignment = _make_assignment(spool_id=8, ams_id=0, tray_id=3)
        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(),  # _find_3mf_by_filename: library search
                MagicMock(),  # _find_3mf_by_filename: archive search
                MagicMock(scalar_one_or_none=MagicMock(return_value=t3_assignment)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=t3_spool)),
            ]
        )

        results = await on_print_complete(1, {"status": "completed"}, pm, db)

        # Only T3 should be charged. T1 (spool 27 in the report) and T2
        # (spool 24) must NOT appear in the results.
        assert len(results) == 1
        assert results[0]["ams_id"] == 0
        assert results[0]["tray_id"] == 3


class TestTrackFrom3MF:
    """Tests for Path 2: 3MF per-filament fallback tracking."""

    @pytest.mark.asyncio
    async def test_updates_non_bl_spool_from_3mf(self):
        """Non-BL spool gets weight_used from 3MF used_g for completed print."""
        spool = _make_spool(id=5, label_weight=1000, weight_used=100)
        assignment = _make_assignment(spool_id=5)
        archive = MagicMock()
        archive.file_path = "archives/test.3mf"

        db = AsyncMock()
        # archive, queue_item(None), assignment, spool
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=assignment)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),
            ]
        )

        pm = _make_printer_manager(_make_printer_state([], tray_now=0))
        filament_usage = [{"slot_id": 1, "used_g": 25.5, "type": "PLA", "color": "#FF0000"}]

        with (
            patch("backend.app.core.config.settings") as mock_settings,
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_settings.base_dir.__truediv__ = MagicMock(return_value=mock_path)

            results = await _track_from_3mf(
                printer_id=1,
                archive_id=10,
                status="completed",
                print_name="test_print",
                handled_trays=set(),
                printer_manager=pm,
                db=db,
            )

        assert len(results) == 1
        assert results[0]["spool_id"] == 5
        assert results[0]["weight_used"] == 25.5
        # weight_used = old (100) + 3MF (25.5)
        assert spool.weight_used == 125.5

    @pytest.mark.asyncio
    async def test_scales_by_progress_for_failed_print(self):
        """Failed print scales 3MF estimate by progress percentage."""
        spool = _make_spool(id=1, label_weight=1000, weight_used=0)
        assignment = _make_assignment()
        archive = MagicMock()
        archive.file_path = "archives/test.3mf"

        db = AsyncMock()
        # archive, queue_item(None), assignment, spool
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=assignment)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),
            ]
        )

        # Print failed at 50% progress → 50g consumed from 100g estimate
        pm = _make_printer_manager(_make_printer_state([], progress=50, tray_now=0))
        filament_usage = [{"slot_id": 1, "used_g": 100.0, "type": "PLA", "color": ""}]

        with (
            patch("backend.app.core.config.settings") as mock_settings,
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_settings.base_dir.__truediv__ = MagicMock(return_value=mock_path)

            results = await _track_from_3mf(
                printer_id=1,
                archive_id=10,
                status="failed",
                print_name="test",
                handled_trays=set(),
                printer_manager=pm,
                db=db,
            )

        assert len(results) == 1
        assert results[0]["weight_used"] == 50.0
        assert spool.weight_used == 50.0

    @pytest.mark.asyncio
    async def test_tracks_bl_spools_via_3mf(self):
        """BL spools (with tag_uid) ARE now tracked via 3MF (unified tracking)."""
        spool = _make_spool(tag_uid="ABCD1234", tray_uuid="A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4")
        assignment = _make_assignment()
        archive = MagicMock()
        archive.file_path = "archives/test.3mf"

        db = AsyncMock()
        # archive, queue_item(None), assignment, spool
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=assignment)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),
            ]
        )

        pm = _make_printer_manager(_make_printer_state([], tray_now=0))
        filament_usage = [{"slot_id": 1, "used_g": 50.0, "type": "PLA", "color": ""}]

        with (
            patch("backend.app.core.config.settings") as mock_settings,
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_settings.base_dir.__truediv__ = MagicMock(return_value=mock_path)

            results = await _track_from_3mf(
                printer_id=1,
                archive_id=10,
                status="completed",
                print_name="test",
                handled_trays=set(),
                printer_manager=pm,
                db=db,
            )

        assert len(results) == 1
        assert results[0]["spool_id"] == 1
        assert results[0]["weight_used"] == 50.0

    @pytest.mark.asyncio
    async def test_skips_already_handled_trays(self):
        """Trays handled by AMS remain% delta are not double-tracked via 3MF."""
        archive = MagicMock()
        archive.file_path = "archives/test.3mf"

        db = AsyncMock()
        # archive, queue_item(None)
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
            ]
        )

        pm = _make_printer_manager(_make_printer_state([], tray_now=0))
        filament_usage = [{"slot_id": 1, "used_g": 50.0, "type": "PLA", "color": ""}]

        with (
            patch("backend.app.core.config.settings") as mock_settings,
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_settings.base_dir.__truediv__ = MagicMock(return_value=mock_path)

            results = await _track_from_3mf(
                printer_id=1,
                archive_id=10,
                status="completed",
                print_name="test",
                handled_trays={(0, 0)},  # slot_id=1 → ams_id=0, tray_id=0
                printer_manager=pm,
                db=db,
            )

        assert results == []

    @pytest.mark.asyncio
    async def test_slot_to_tray_mapping(self):
        """3MF slot_id maps correctly to (ams_id, tray_id) via tray_now."""
        # tray_now=4 → ams_id=1, tray_id=0 (single filament uses tray_now)
        spool = _make_spool(id=9)
        assignment = _make_assignment(spool_id=9, ams_id=1, tray_id=0)
        archive = MagicMock()
        archive.file_path = "archives/test.3mf"

        db = AsyncMock()
        # archive, queue_item(None), assignment, spool
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=assignment)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),
            ]
        )

        pm = _make_printer_manager(_make_printer_state([], tray_now=4))
        filament_usage = [{"slot_id": 5, "used_g": 30.0, "type": "PETG", "color": ""}]

        with (
            patch("backend.app.core.config.settings") as mock_settings,
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_settings.base_dir.__truediv__ = MagicMock(return_value=mock_path)

            results = await _track_from_3mf(
                printer_id=1,
                archive_id=10,
                status="completed",
                print_name="test",
                handled_trays=set(),
                printer_manager=pm,
                db=db,
            )

        assert len(results) == 1
        assert results[0]["ams_id"] == 1
        assert results[0]["tray_id"] == 0


class TestTaglessAmsAutoSpools:
    """Auto-minted tagless spools (data_origin='ams_auto') in usage tracking.

    The tagless lifecycle mints full spool records for non-RFID trays; those
    rows must flow through Path-1 (3MF per-filament grams) like any bound
    spool, and the remain%-delta fallback must NEVER charge them — tagless
    trays report remain=-1 (firmware "unknown"), which the :594 guard rejects
    before any DB lookup.
    """

    @pytest.fixture(autouse=True)
    def _clear_sessions(self):
        _active_sessions.clear()
        yield
        _active_sessions.clear()

    @pytest.fixture(autouse=True)
    def _mock_get_setting(self):
        with patch(
            "backend.app.api.routes.settings.get_setting",
            new_callable=AsyncMock,
            return_value=None,
        ):
            yield

    @pytest.mark.asyncio
    async def test_updates_ams_auto_tagless_spool_from_3mf(self):
        """An auto-minted tagless spool gets the 3MF Path-1 decrement."""
        spool = _make_spool(id=9, label_weight=1000, weight_used=40, tag_uid=None, tray_uuid=None)
        spool.data_origin = "ams_auto"
        assignment = _make_assignment(spool_id=9)
        archive = MagicMock()
        archive.file_path = "archives/tagless.3mf"

        db = AsyncMock()
        # archive, queue_item(None), assignment, spool
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=assignment)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),
            ]
        )

        pm = _make_printer_manager(_make_printer_state([], tray_now=0))
        filament_usage = [{"slot_id": 1, "used_g": 30.0, "type": "PETG", "color": "#000000"}]

        with (
            patch("backend.app.core.config.settings") as mock_settings,
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_settings.base_dir.__truediv__ = MagicMock(return_value=mock_path)

            results = await _track_from_3mf(
                printer_id=1,
                archive_id=10,
                status="completed",
                print_name="tagless_print",
                handled_trays=set(),
                printer_manager=pm,
                db=db,
            )

        assert len(results) == 1
        assert results[0]["spool_id"] == 9
        assert results[0]["weight_used"] == 30.0
        # weight_used = old (40) + 3MF (30)
        assert spool.weight_used == 70.0

    @pytest.mark.asyncio
    async def test_remain_fallback_never_charges_tagless_unknown_remain(self):
        """remain=-1 at completion (tagless tray) skips the fallback entirely.

        Even with a plausible start snapshot and the tray in the print's
        mapping, the invalid-remain guard must bail before any assignment or
        spool DB lookup is issued — an ams_auto spool is never charged by the
        remain%-delta path.
        """
        _active_sessions[1] = PrintSession(
            printer_id=1,
            print_name="tagless",
            started_at=datetime.now(timezone.utc),
            tray_remain_start={(0, 0): 100},
            tray_now_at_start=0,
            ams_mapping=[0],
        )

        # Tagless tray reports remain=-1 (firmware unknown) at completion.
        ams_data = [{"id": 0, "tray": [{"id": 0, "remain": -1}]}]
        state = _make_printer_state(ams_data, tray_now=0)
        state.tray_change_log = [(0, 0)]
        pm = _make_printer_manager(state)

        db = AsyncMock()
        # Only the two _find_3mf_by_filename searches may hit the DB; the
        # remain guard must skip before the assignment/spool lookups.
        db.execute = AsyncMock(side_effect=[MagicMock(), MagicMock()])

        results = await on_print_complete(1, {"status": "completed"}, pm, db)

        assert results == []
        db.commit.assert_not_called()
        # Guard fired before any assignment/spool query (only 3MF searches ran).
        assert db.execute.await_count <= 2


class TestSpoolAssignmentSnapshot:
    """Tests for spool assignment snapshotting at print start (#459).

    When a spool runs empty mid-print, on_ams_change deletes the SpoolAssignment.
    The snapshot captured at print start ensures usage is still attributed correctly.
    """

    @pytest.fixture(autouse=True)
    def _clear_sessions(self):
        _active_sessions.clear()
        yield
        _active_sessions.clear()

    @pytest.fixture(autouse=True)
    def _mock_get_setting(self):
        with patch(
            "backend.app.api.routes.settings.get_setting",
            new_callable=AsyncMock,
            return_value=None,
        ):
            yield

    @pytest.mark.asyncio
    async def test_on_print_start_snapshots_assignments_with_db(self):
        """on_print_start captures spool assignments when db is provided."""
        ams_data = [{"id": 0, "tray": [{"id": 0, "remain": 80}, {"id": 1, "remain": 60}]}]
        pm = _make_printer_manager(_make_printer_state(ams_data, tray_now=0))

        assignment_0 = _make_assignment(spool_id=10, printer_id=1, ams_id=0, tray_id=0)
        assignment_1 = _make_assignment(spool_id=20, printer_id=1, ams_id=0, tray_id=1)

        db = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [assignment_0, assignment_1]
        result_mock = MagicMock()
        result_mock.scalars.return_value = scalars_mock
        db.execute = AsyncMock(return_value=result_mock)

        await on_print_start(1, {"subtask_name": "Benchy"}, pm, db=db)

        session = _active_sessions[1]
        assert session.spool_assignments == {(0, 0): 10, (0, 1): 20}

    @pytest.mark.asyncio
    async def test_on_print_start_empty_snapshot_without_db(self):
        """on_print_start creates empty snapshot when no db provided."""
        ams_data = [{"id": 0, "tray": [{"id": 0, "remain": 80}]}]
        pm = _make_printer_manager(_make_printer_state(ams_data, tray_now=0))

        await on_print_start(1, {"subtask_name": "Benchy"}, pm)

        session = _active_sessions[1]
        assert session.spool_assignments == {}

    @pytest.mark.asyncio
    async def test_3mf_uses_snapshot_instead_of_live_query(self):
        """_track_from_3mf uses snapshot spool_id without querying SpoolAssignment."""
        spool = _make_spool(id=42, label_weight=1000)
        archive = MagicMock()
        archive.file_path = "archives/test.3mf"

        # db: archive, queue_item(None), spool — NO assignment query needed
        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),
            ]
        )

        pm = _make_printer_manager(_make_printer_state([], tray_now=0))
        filament_usage = [{"slot_id": 1, "used_g": 15.0, "type": "PLA", "color": "#FF0000"}]

        with (
            patch("backend.app.core.config.settings") as mock_settings,
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_settings.base_dir.__truediv__ = MagicMock(return_value=mock_path)

            results = await _track_from_3mf(
                printer_id=1,
                archive_id=10,
                status="completed",
                print_name="Test",
                handled_trays=set(),
                printer_manager=pm,
                db=db,
                spool_assignments={(0, 0): 42},
            )

        assert len(results) == 1
        assert results[0]["spool_id"] == 42
        assert results[0]["weight_used"] == 15.0

    @pytest.mark.asyncio
    async def test_3mf_falls_back_to_live_query_without_snapshot(self):
        """_track_from_3mf queries SpoolAssignment when no snapshot exists."""
        spool = _make_spool(id=5, label_weight=1000)
        assignment = _make_assignment(spool_id=5)
        archive = MagicMock()
        archive.file_path = "archives/test.3mf"

        # db: archive, queue_item(None), assignment, spool
        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=assignment)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),
            ]
        )

        pm = _make_printer_manager(_make_printer_state([], tray_now=0))
        filament_usage = [{"slot_id": 1, "used_g": 10.0, "type": "PLA", "color": "#FF0000"}]

        with (
            patch("backend.app.core.config.settings") as mock_settings,
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_settings.base_dir.__truediv__ = MagicMock(return_value=mock_path)

            results = await _track_from_3mf(
                printer_id=1,
                archive_id=10,
                status="completed",
                print_name="Test",
                handled_trays=set(),
                printer_manager=pm,
                db=db,
                spool_assignments=None,
            )

        assert len(results) == 1
        assert results[0]["spool_id"] == 5

    @pytest.mark.asyncio
    async def test_ams_delta_uses_snapshot_over_live_query(self):
        """AMS remain% fallback uses snapshot spool_id instead of live query."""
        spool = _make_spool(id=77, label_weight=1000)

        _active_sessions[1] = PrintSession(
            printer_id=1,
            print_name="Benchy",
            started_at=datetime.now(timezone.utc),
            tray_remain_start={(0, 0): 80},
            spool_assignments={(0, 0): 77},
        )

        # Current remain = 70% → 10% delta → 100g
        ams_data = [{"id": 0, "tray": [{"id": 0, "remain": 70}]}]
        pm = _make_printer_manager(_make_printer_state(ams_data))

        # First 2 executes → _find_3mf_by_filename (library + archive search),
        # then live assignment check (returns None), then spool lookup by snapshot spool_id
        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(),  # _find_3mf_by_filename: library search
                MagicMock(),  # _find_3mf_by_filename: archive search
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),  # live assignment
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),
            ]
        )

        results = await on_print_complete(
            printer_id=1,
            data={"status": "completed"},
            printer_manager=pm,
            db=db,
            archive_id=None,
        )

        assert len(results) == 1
        assert results[0]["spool_id"] == 77
        assert results[0]["weight_used"] == 100.0

    @pytest.mark.asyncio
    async def test_ams_delta_falls_back_to_live_query_without_snapshot(self):
        """AMS remain% fallback queries SpoolAssignment when snapshot is empty."""
        spool = _make_spool(id=33, label_weight=1000)
        assignment = _make_assignment(spool_id=33)

        _active_sessions[1] = PrintSession(
            printer_id=1,
            print_name="Benchy",
            started_at=datetime.now(timezone.utc),
            tray_remain_start={(0, 0): 80},
            spool_assignments={},  # Empty snapshot (pre-upgrade session)
        )

        ams_data = [{"id": 0, "tray": [{"id": 0, "remain": 70}]}]
        pm = _make_printer_manager(_make_printer_state(ams_data))

        # First 2 executes → _find_3mf_by_filename (library + archive search),
        # then assignment and spool for the AMS fallback path
        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(),  # _find_3mf_by_filename: library search
                MagicMock(),  # _find_3mf_by_filename: archive search
                MagicMock(scalar_one_or_none=MagicMock(return_value=assignment)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),
            ]
        )

        results = await on_print_complete(
            printer_id=1,
            data={"status": "completed"},
            printer_manager=pm,
            db=db,
            archive_id=None,
        )

        assert len(results) == 1
        assert results[0]["spool_id"] == 33

    @pytest.mark.asyncio
    async def test_snapshot_survives_mid_print_unlink(self):
        """Core bug scenario: snapshot provides spool_id after mid-print unlink.

        Simulates the #459 scenario: spool runs empty mid-print, on_ams_change
        deletes the SpoolAssignment, but the snapshot from print start still
        has the spool_id so usage is correctly attributed at print completion.
        """
        spool = _make_spool(id=8, label_weight=1000, weight_used=50)
        archive = MagicMock()
        archive.file_path = "archives/big_print.3mf"
        # Explicit numeric so the #1344 top-up branch doesn't trip a
        # MagicMock-vs-float comparison.
        archive.filament_used_grams = 14.2

        # Session was created at print start WITH snapshot
        _active_sessions[1] = PrintSession(
            printer_id=1,
            print_name="Big Print",
            started_at=datetime.now(timezone.utc),
            tray_remain_start={(0, 0): 90},
            spool_assignments={(0, 0): 8},  # Snapshot from print start
        )

        pm = _make_printer_manager(
            _make_printer_state(
                [{"id": 0, "tray": [{"id": 0, "remain": 75}]}],
                tray_now=0,
            )
        )

        filament_usage = [{"slot_id": 1, "used_g": 14.2, "type": "PLA", "color": "#FF0000"}]

        # db: guard(archive.started_at, usage-count), then archive, queue_item(None),
        # live assignment(None), spool, then cost aggregation queries.
        # NOTE: No assignment in db — it was deleted by on_ams_change mid-print!
        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                # Idempotency guard: started_at load + usage-count (non-int -> no rows).
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
                MagicMock(),
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),
                # Cost-update block re-selects the archive to mutate cost.
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
            ]
        )

        with (
            patch("backend.app.core.config.settings") as mock_settings,
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_settings.base_dir.__truediv__ = MagicMock(return_value=mock_path)

            results = await on_print_complete(
                printer_id=1,
                data={"status": "completed"},
                printer_manager=pm,
                db=db,
                archive_id=100,
            )

        # Usage should be tracked despite assignment being deleted mid-print
        assert len(results) >= 1
        assert results[0]["spool_id"] == 8
        assert results[0]["weight_used"] == 14.2
        # Spool weight should be updated: 50 + 14.2 = 64.2
        assert spool.weight_used == 64.2


class TestSpoolColorToHex:
    """`_spool_color_to_hex` normalises Spool.rgba (RRGGBBAA, no #) to #RRGGBB."""

    def test_strips_alpha_and_adds_hash(self):
        assert _spool_color_to_hex("000000FF") == "#000000"
        assert _spool_color_to_hex("EC984CFF") == "#EC984C"

    def test_uppercases(self):
        assert _spool_color_to_hex("ec984cff") == "#EC984C"

    def test_accepts_six_char_value(self):
        """A value with no alpha is still valid."""
        assert _spool_color_to_hex("161616") == "#161616"

    def test_tolerates_leading_hash(self):
        assert _spool_color_to_hex("#000000FF") == "#000000"

    def test_none_and_too_short_return_none(self):
        """Missing / malformed colour falls back to the 3MF value."""
        assert _spool_color_to_hex(None) is None
        assert _spool_color_to_hex("") is None
        assert _spool_color_to_hex("FFF") is None


class TestArchiveColorsFromSpools:
    """`_archive_colors_from_spools` rebuilds an archive's filament_color from
    the inventory spools that fed the print (#1494). All-or-nothing: a partial
    match returns None so the 3MF colour is left intact."""

    def test_single_slot_matched(self):
        """The #1494 case: one used slot, matched to a #000000 spool."""
        usage = [{"slot_id": 1, "used_g": 15.9, "color": "#161616"}]
        results = [{"slot_id": 1, "color": "#000000"}]
        assert _archive_colors_from_spools(usage, results) == ["#000000"]

    def test_multi_slot_all_matched_keeps_slot_order(self):
        usage = [
            {"slot_id": 1, "used_g": 10.0, "color": "#111111"},
            {"slot_id": 2, "used_g": 20.0, "color": "#222222"},
        ]
        # results deliberately out of slot order — output must be slot-ordered
        results = [
            {"slot_id": 2, "color": "#00FF00"},
            {"slot_id": 1, "color": "#FF0000"},
        ]
        assert _archive_colors_from_spools(usage, results) == ["#FF0000", "#00FF00"]

    def test_duplicate_colors_deduplicated(self):
        """Two slots of the same spool colour collapse to one entry, as the
        3MF-derived path also de-duplicates."""
        usage = [
            {"slot_id": 1, "used_g": 10.0, "color": "#111111"},
            {"slot_id": 2, "used_g": 20.0, "color": "#222222"},
        ]
        results = [
            {"slot_id": 1, "color": "#000000"},
            {"slot_id": 2, "color": "#000000"},
        ]
        assert _archive_colors_from_spools(usage, results) == ["#000000"]

    def test_partial_match_returns_none(self):
        """Slot 2 was used but never matched to a spool — leave the 3MF colour
        untouched rather than dropping slot 2 from the archive."""
        usage = [
            {"slot_id": 1, "used_g": 10.0, "color": "#111111"},
            {"slot_id": 2, "used_g": 20.0, "color": "#222222"},
        ]
        results = [{"slot_id": 1, "color": "#000000"}]
        assert _archive_colors_from_spools(usage, results) is None

    def test_matched_spool_without_color_returns_none(self):
        """A spool with no rgba (color None) does not count as matched."""
        usage = [{"slot_id": 1, "used_g": 15.0, "color": "#161616"}]
        results = [{"slot_id": 1, "color": None}]
        assert _archive_colors_from_spools(usage, results) is None

    def test_unused_slot_not_required(self):
        """A slot with zero usage need not be matched."""
        usage = [
            {"slot_id": 1, "used_g": 15.0, "color": "#161616"},
            {"slot_id": 2, "used_g": 0.0, "color": "#888888"},
        ]
        results = [{"slot_id": 1, "color": "#000000"}]
        assert _archive_colors_from_spools(usage, results) == ["#000000"]

    def test_no_used_slots_returns_none(self):
        assert _archive_colors_from_spools([], []) is None

    def test_ams_fallback_results_excluded(self):
        """AMS remain%-delta fallback results carry slot_id=None and must not
        satisfy the match for a real 3MF slot."""
        usage = [{"slot_id": 1, "used_g": 15.0, "color": "#161616"}]
        results = [{"slot_id": None, "color": "#000000"}]
        assert _archive_colors_from_spools(usage, results) is None


class TestArchiveFilamentColorRewrite:
    """`_track_from_3mf` overwrites the archive's filament_color with the
    matched inventory spool colour at print completion (#1494)."""

    @pytest.mark.asyncio
    async def test_archive_color_adopts_spool_color(self):
        """A print from a #000000 inventory spool whose 3MF says #161616 ends
        up with the archive showing the spool's #000000."""
        spool = _make_spool(id=5, label_weight=1000, weight_used=100, rgba="000000FF")
        assignment = _make_assignment(spool_id=5)
        archive = MagicMock()
        archive.file_path = "archives/test.3mf"
        archive.filament_color = "#161616"  # what archive.py set from the 3MF

        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=assignment)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),
            ]
        )

        pm = _make_printer_manager(_make_printer_state([], tray_now=0))
        filament_usage = [{"slot_id": 1, "used_g": 25.5, "type": "PETG", "color": "#161616"}]

        with (
            patch("backend.app.core.config.settings") as mock_settings,
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_settings.base_dir.__truediv__ = MagicMock(return_value=mock_path)

            results = await _track_from_3mf(
                printer_id=1,
                archive_id=10,
                status="completed",
                print_name="test_print",
                handled_trays=set(),
                printer_manager=pm,
                db=db,
            )

        assert len(results) == 1
        assert results[0]["color"] == "#000000"
        assert results[0]["slot_id"] == 1
        # The archive colour was rewritten from the slicer's #161616 to the
        # inventory spool's #000000.
        assert archive.filament_color == "#000000"

    @pytest.mark.asyncio
    async def test_archive_color_untouched_when_spool_has_no_color(self):
        """A spool with no rgba leaves the 3MF colour in place."""
        spool = _make_spool(id=5, label_weight=1000, weight_used=100, rgba=None)
        assignment = _make_assignment(spool_id=5)
        archive = MagicMock()
        archive.file_path = "archives/test.3mf"
        archive.filament_color = "#161616"

        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=assignment)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),
            ]
        )

        pm = _make_printer_manager(_make_printer_state([], tray_now=0))
        filament_usage = [{"slot_id": 1, "used_g": 25.5, "type": "PETG", "color": "#161616"}]

        with (
            patch("backend.app.core.config.settings") as mock_settings,
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_settings.base_dir.__truediv__ = MagicMock(return_value=mock_path)

            await _track_from_3mf(
                printer_id=1,
                archive_id=10,
                status="completed",
                print_name="test_print",
                handled_trays=set(),
                printer_manager=pm,
                db=db,
            )

        assert archive.filament_color == "#161616"


def _split_state(tray_change_log, *, total_layers=100, progress=100, layer_num=100, tray_now=0, last_loaded_tray=0):
    """Printer state carrying a REAL tray_change_log list for split tests.

    ``_make_printer_state`` returns a bare MagicMock whose ``tray_change_log`` is
    an auto-attr (not a list), which the tracker's isinstance guard treats as
    empty — good for the no-split cases but useless when a split is the point.
    """
    return SimpleNamespace(
        raw_data={},
        tray_change_log=list(tray_change_log),
        total_layers=total_layers,
        progress=progress,
        layer_num=layer_num,
        tray_now=tray_now,
        last_loaded_tray=last_loaded_tray,
    )


def _mock_settings_path():
    """Patched app settings whose base_dir / anything resolves to an existing path."""
    mock_settings = MagicMock()
    mock_path = MagicMock()
    mock_path.exists.return_value = True
    mock_settings.base_dir.__truediv__ = MagicMock(return_value=mock_path)
    return mock_settings, mock_path


class TestAssignSegmentsToSlots:
    """`_assign_segments_to_slots` groups the whole-print tray change log per slot."""

    def test_single_filament_gets_all_segments(self):
        # One active filament → every segment fed it, mapping irrelevant.
        assert _assign_segments_to_slots([(0, 0), (1, 30), (3, 60)], None, [1]) == {1: [(0, 0), (1, 30), (3, 60)]}

    def test_empty_log_returns_empty(self):
        assert _assign_segments_to_slots([], [0, 1], [1, 2]) == {}

    def test_multi_filament_reverse_maps_and_orphan_inherits(self):
        # slot1→tray0, slot2→tray1; tray3 is an unmapped backup that engaged
        # while slot1 (tray0) was feeding → it inherits slot1.
        out = _assign_segments_to_slots([(0, 0), (1, 20), (0, 40), (3, 60)], [0, 1], [1, 2])
        assert out[1] == [(0, 0), (0, 40), (3, 60)]
        assert out[2] == [(1, 20)]

    def test_multi_filament_without_mapping_returns_empty(self):
        # No slicer mapping to attribute segments → refuse to guess.
        assert _assign_segments_to_slots([(0, 0), (1, 30)], None, [1, 2]) == {}

    def test_zero_usage_slot_dropped(self):
        # Multi-filament: slot2 (tray1) has zero usage → its segment is dropped
        # while slot1 and slot3 keep theirs. (A single nonzero slot instead takes
        # the "all segments feed it" fast path, mapping-independent.)
        out = _assign_segments_to_slots([(0, 0), (1, 20), (2, 40)], [0, 1, 2], [1, 3])
        assert out == {1: [(0, 0)], 3: [(2, 40)]}


class TestMultiFeederSplitAllPaths:
    """The per-feeder split fires in the shared `_track_from_3mf` (used by the
    primary queue path, the 3MF fallback path, reconcile, and foreign prints),
    for single- AND multi-filament prints."""

    @pytest.mark.asyncio
    async def test_single_filament_multi_feeder_splits_by_layer_span(self):
        """AMS backup: one colour fed from tray0 then tray1 splits proportionally.

        Reproduces the 006 shape — a spool runs dry mid-print and the AMS auto-
        refills from a sibling tray; each tray's spool must be charged its span,
        not the whole print dumped on the print-start mapped slot.
        """
        spool0 = _make_spool(id=10, label_weight=1000, weight_used=0)
        spool1 = _make_spool(id=20, label_weight=1000, weight_used=0)
        assign0 = _make_assignment(spool_id=10, ams_id=0, tray_id=0)
        assign1 = _make_assignment(spool_id=20, ams_id=0, tray_id=1)
        archive = MagicMock()
        archive.file_path = "archives/backup.3mf"
        archive.filament_color = None

        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=assign0)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool0)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=assign1)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool1)),
            ]
        )

        pm = _make_printer_manager(_split_state([(0, 0), (1, 40)], tray_now=1, last_loaded_tray=1))
        filament_usage = [{"slot_id": 1, "used_g": 100.0, "type": "PLA", "color": ""}]

        mock_settings, _ = _mock_settings_path()
        with (
            patch("backend.app.core.config.settings", mock_settings),
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
            patch("backend.app.utils.threemf_tools.extract_layer_filament_usage_from_3mf", return_value=None),
        ):
            results = await _track_from_3mf(
                printer_id=1,
                archive_id=10,
                status="completed",
                print_name="backup",
                handled_trays=set(),
                printer_manager=pm,
                db=db,
                ams_mapping=[0],  # slicer said tray0 — but the printer fed tray1 too
            )

        # tray0 got layers 0-40 (40 g), tray1 the remainder (60 g).
        by_tray = {(r["ams_id"], r["tray_id"]): r for r in results}
        assert by_tray[(0, 0)]["weight_used"] == 40.0
        assert by_tray[(0, 0)]["spool_id"] == 10
        assert by_tray[(0, 1)]["weight_used"] == 60.0
        assert by_tray[(0, 1)]["spool_id"] == 20
        assert spool0.weight_used == 40.0
        assert spool1.weight_used == 60.0
        # Both fed spools stamped last_used.
        assert spool0.last_used is not None and spool1.last_used is not None

    @pytest.mark.asyncio
    async def test_single_feeder_unchanged(self):
        """A one-entry change log is not a split — full weight to the one tray."""
        spool = _make_spool(id=7, label_weight=1000, weight_used=0)
        assign = _make_assignment(spool_id=7, ams_id=0, tray_id=2)
        archive = MagicMock()
        archive.file_path = "archives/single.3mf"
        archive.filament_color = None

        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=assign)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),
            ]
        )

        pm = _make_printer_manager(_split_state([(2, 0)], tray_now=2, last_loaded_tray=2))
        filament_usage = [{"slot_id": 1, "used_g": 55.0, "type": "PLA", "color": ""}]

        mock_settings, _ = _mock_settings_path()
        with (
            patch("backend.app.core.config.settings", mock_settings),
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
            results = await _track_from_3mf(
                printer_id=1,
                archive_id=10,
                status="completed",
                print_name="single",
                handled_trays=set(),
                printer_manager=pm,
                db=db,
                ams_mapping=[2],
            )

        assert len(results) == 1
        assert results[0]["ams_id"] == 0 and results[0]["tray_id"] == 2
        assert results[0]["weight_used"] == 55.0
        assert spool.weight_used == 55.0

    @pytest.mark.asyncio
    async def test_multi_filament_backup_splits_per_slot(self):
        """Multi-colour print: slot1 backup-switches (tray0→tray3), slot2 steady.

        The old `len(nonzero_slots) == 1` gate skipped the split entirely for any
        print using >=2 filament slots, so slot1's backup roll (tray3) was charged
        nothing and its share was dumped on tray0. Now slot1 splits across its own
        feeders while slot2 charges only its mapped tray — no cross-slot leakage.
        """
        spool0 = _make_spool(id=10, label_weight=1000, weight_used=0)  # slot1 home tray0
        spool3 = _make_spool(id=30, label_weight=1000, weight_used=0)  # slot1 backup tray3
        spool1 = _make_spool(id=20, label_weight=1000, weight_used=0)  # slot2 tray1
        assign0 = _make_assignment(spool_id=10, ams_id=0, tray_id=0)
        assign3 = _make_assignment(spool_id=30, ams_id=0, tray_id=3)
        assign1 = _make_assignment(spool_id=20, ams_id=0, tray_id=1)
        archive = MagicMock()
        archive.file_path = "archives/twocolor.3mf"
        archive.filament_color = None

        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
                # slot1 split: tray0 then tray3 (per_tray insertion order)
                MagicMock(scalar_one_or_none=MagicMock(return_value=assign0)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool0)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=assign3)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool3)),
                # slot2 normal path: tray1
                MagicMock(scalar_one_or_none=MagicMock(return_value=assign1)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool1)),
            ]
        )

        pm = _make_printer_manager(_split_state([(0, 0), (1, 20), (0, 40), (3, 60)], tray_now=3, last_loaded_tray=3))
        filament_usage = [
            {"slot_id": 1, "used_g": 60.0, "type": "PLA", "color": "#FF0000"},
            {"slot_id": 2, "used_g": 40.0, "type": "PLA", "color": "#00FF00"},
        ]

        mock_settings, _ = _mock_settings_path()
        with (
            patch("backend.app.core.config.settings", mock_settings),
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
            patch("backend.app.utils.threemf_tools.extract_layer_filament_usage_from_3mf", return_value=None),
        ):
            results = await _track_from_3mf(
                printer_id=1,
                archive_id=10,
                status="completed",
                print_name="twocolor",
                handled_trays=set(),
                printer_manager=pm,
                db=db,
                ams_mapping=[0, 1],  # slot1→tray0, slot2→tray1
            )

        by_tray = {(r["ams_id"], r["tray_id"]): r for r in results}
        # slot1 (60 g) split across its feeders tray0 (0-60 linear) and tray3 (remainder).
        assert by_tray[(0, 0)]["slot_id"] == 1
        assert by_tray[(0, 3)]["slot_id"] == 1
        assert round(by_tray[(0, 0)]["weight_used"] + by_tray[(0, 3)]["weight_used"], 1) == 60.0
        assert by_tray[(0, 3)]["weight_used"] > 0  # backup roll is no longer 0 g
        assert spool3.weight_used > 0
        # slot2 (40 g) charged only to its own tray1 — no leakage from slot1's split.
        assert by_tray[(0, 1)]["slot_id"] == 2
        assert by_tray[(0, 1)]["weight_used"] == 40.0
        assert spool1.weight_used == 40.0


class TestForeignPrintCharging:
    """R3: a foreign / screen-started print (no farm queue item) still reaches the
    same 3MF charging path — accounting only, no farm-unit or gate side effects."""

    @pytest.fixture(autouse=True)
    def _clear_sessions(self):
        _active_sessions.clear()
        yield
        _active_sessions.clear()

    @pytest.fixture(autouse=True)
    def _mock_get_setting(self):
        with patch(
            "backend.app.api.routes.settings.get_setting",
            new_callable=AsyncMock,
            return_value=None,
        ):
            yield

    @pytest.mark.asyncio
    async def test_foreign_single_feeder_charges_observed_tray(self):
        """No ams_mapping and no queue item — the feeder resolves from live tray_now."""
        spool = _make_spool(id=9, label_weight=1000, weight_used=0)
        assign = _make_assignment(spool_id=9, ams_id=0, tray_id=0)
        archive = MagicMock()
        archive.file_path = "archives/foreign.3mf"
        archive.filament_color = None

        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),  # no queue item
                MagicMock(scalar_one_or_none=MagicMock(return_value=assign)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),
            ]
        )

        # tray_now_at_start feeds the single-filament fallback; no queue mapping.
        pm = _make_printer_manager(_split_state([(0, 0)], tray_now=0, last_loaded_tray=0))
        filament_usage = [{"slot_id": 1, "used_g": 33.0, "type": "PLA", "color": ""}]

        mock_settings, _ = _mock_settings_path()
        with (
            patch("backend.app.core.config.settings", mock_settings),
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
            results = await _track_from_3mf(
                printer_id=1,
                archive_id=10,
                status="completed",
                print_name="foreign_lan_print",
                handled_trays=set(),
                printer_manager=pm,
                db=db,
                ams_mapping=None,  # foreign print: nothing dispatched it
                tray_now_at_start=0,
            )

        assert len(results) == 1
        assert results[0]["ams_id"] == 0 and results[0]["tray_id"] == 0
        assert results[0]["weight_used"] == 33.0
        assert spool.weight_used == 33.0

    @pytest.mark.asyncio
    async def test_foreign_multi_feeder_splits(self):
        """A foreign single-colour print fed from two trays splits identically —
        the split needs no queue mapping (single active filament)."""
        spool0 = _make_spool(id=11, label_weight=1000, weight_used=0)
        spool1 = _make_spool(id=12, label_weight=1000, weight_used=0)
        assign0 = _make_assignment(spool_id=11, ams_id=0, tray_id=0)
        assign1 = _make_assignment(spool_id=12, ams_id=0, tray_id=1)
        archive = MagicMock()
        archive.file_path = "archives/foreign_backup.3mf"
        archive.filament_color = None

        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),  # no queue item
                MagicMock(scalar_one_or_none=MagicMock(return_value=assign0)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool0)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=assign1)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool1)),
            ]
        )

        pm = _make_printer_manager(_split_state([(0, 0), (1, 25)], tray_now=1, last_loaded_tray=1))
        filament_usage = [{"slot_id": 1, "used_g": 80.0, "type": "PLA", "color": ""}]

        mock_settings, _ = _mock_settings_path()
        with (
            patch("backend.app.core.config.settings", mock_settings),
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
            patch("backend.app.utils.threemf_tools.extract_layer_filament_usage_from_3mf", return_value=None),
        ):
            results = await _track_from_3mf(
                printer_id=1,
                archive_id=10,
                status="completed",
                print_name="foreign_backup",
                handled_trays=set(),
                printer_manager=pm,
                db=db,
                ams_mapping=None,
            )

        by_tray = {(r["ams_id"], r["tray_id"]): r for r in results}
        assert set(by_tray) == {(0, 0), (0, 1)}
        assert round(sum(r["weight_used"] for r in results), 1) == 80.0
        assert by_tray[(0, 0)]["weight_used"] == 20.0  # layers 0-25 of 100
        assert by_tray[(0, 1)]["weight_used"] == 60.0  # remainder
        assert spool0.weight_used == 20.0 and spool1.weight_used == 60.0

    @pytest.mark.asyncio
    async def test_foreign_completion_charges_without_farm_unit_mutation(self):
        """End-to-end via on_print_complete: a foreign session (plate_id=None,
        no ams_mapping) charges the spool and adds ONLY SpoolUsageHistory rows —
        never a PrintQueueItem — so farm queue state is untouched."""
        from backend.app.models.print_queue import PrintQueueItem

        spool = _make_spool(id=5, label_weight=1000, weight_used=0)
        assign = _make_assignment(spool_id=5, ams_id=0, tray_id=0)
        archive = MagicMock()
        archive.file_path = "archives/foreign_e2e.3mf"
        archive.filament_color = "#000000"
        archive.filament_used_grams = 30.0  # == tracked → no untracked top-up

        # Foreign print: session exists (on_print_start runs for every non-eject
        # print) but carries no plate_id / ams_mapping and no farm queue linkage.
        _active_sessions[1] = PrintSession(
            printer_id=1,
            print_name="foreign_e2e",
            started_at=datetime.now(timezone.utc),
            tray_remain_start={},  # skip the remain%-delta fallback
            tray_now_at_start=0,
            spool_assignments={},
            ams_mapping=None,
            plate_id=None,
        )

        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),  # idempotency: started_at None
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),  # _track archive
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),  # no queue item
                MagicMock(scalar_one_or_none=MagicMock(return_value=assign)),  # live assignment
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),  # spool
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),  # cost re-select
            ]
        )

        pm = _make_printer_manager(_split_state([(0, 0)], tray_now=0, last_loaded_tray=0))
        filament_usage = [{"slot_id": 1, "used_g": 30.0, "type": "PLA", "color": ""}]

        mock_settings, _ = _mock_settings_path()
        with (
            patch("backend.app.core.config.settings", mock_settings),
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
            patch("backend.app.utils.threemf_tools.count_plates_in_slice_info", return_value=1),
        ):
            results = await on_print_complete(1, {"status": "completed"}, pm, db, archive_id=100)

        # The foreign print WAS charged.
        assert len(results) == 1
        assert results[0]["spool_id"] == 5
        assert results[0]["weight_used"] == 30.0
        assert spool.weight_used == 30.0
        # No farm-unit mutation: only SpoolUsageHistory rows were added, never a
        # PrintQueueItem (usage tracking is pure accounting).
        added = [c.args[0] for c in db.add.call_args_list]
        assert added, "expected a usage-history row"
        assert all(isinstance(obj, SpoolUsageHistory) for obj in added)
        assert not any(isinstance(obj, PrintQueueItem) for obj in added)


class TestIdempotencyGuard:
    """A duplicate completion (reconcile racing the MQTT terminal, or a manual
    re-finalize) must not double-charge."""

    @pytest.fixture(autouse=True)
    def _clear_sessions(self):
        _active_sessions.clear()
        yield
        _active_sessions.clear()

    @pytest.fixture(autouse=True)
    def _mock_get_setting(self):
        with patch(
            "backend.app.api.routes.settings.get_setting",
            new_callable=AsyncMock,
            return_value=None,
        ):
            yield

    @pytest.mark.asyncio
    async def test_duplicate_completion_is_noop(self):
        """A usage-history row at/after the archive's started_at means THIS run
        already finalized → the second completion returns [] and charges nothing."""
        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                # started_at load (non-None) then usage-count >= started_at → 1.
                MagicMock(scalar_one_or_none=MagicMock(return_value=datetime.now(timezone.utc))),
                MagicMock(scalar=MagicMock(return_value=1)),
            ]
        )
        pm = _make_printer_manager(_split_state([(0, 0)]))

        results = await on_print_complete(1, {"status": "completed"}, pm, db, archive_id=100)

        assert results == []
        db.commit.assert_not_called()
        # Bailed at the guard: only the two guard queries ran, no spool lookups.
        assert db.execute.await_count == 2
