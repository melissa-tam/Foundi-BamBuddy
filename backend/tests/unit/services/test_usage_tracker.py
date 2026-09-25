"""Unit tests for the filament usage tracker (`services/usage_tracker.py`).

Covers the whole charging pipeline: session capture at print start, 3MF-primary
tracking, AMS remain%-delta fallback, per-layer scaling for partial prints, the
slot -> tray resolution ladder, mid-print feeder splits, foreign-print charging
and the wire-identity gate on ledger writes.
"""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import usage_tracker as usage_tracker_module
from backend.app.services.usage_tracker import (
    PrintSession,
    _active_sessions,
    _archive_colors_from_spools,
    _assign_segments_to_slots,
    _decode_mqtt_mapping,
    _find_3mf_by_filename,
    _match_slots_by_color,
    _parse_ams_mapping,
    _resolve_run_context,
    _spool_color_to_hex,
    _track_from_3mf,
    on_print_complete,
    on_print_start,
    wire_identity_is_the_bound_row,
)
from backend.app.utils.threemf_tools import count_plates_in_slice_info


def _make_spool(*, spool_id=1, label_weight=1000, weight_used=0, tag_uid=None, tray_uuid=None, rgba=None):
    """Create a mock Spool object."""
    spool = MagicMock()
    spool.id = spool_id
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


def _write_3mf_with_plates(path, plates: dict[int, list[tuple]]):
    """Write a minimal .gcode.3mf whose Metadata/slice_info.config carries the
    given plates. ``plates`` maps plate index -> list of
    ``(filament_id, used_g, type, color)`` tuples.
    """
    import zipfile as _zip

    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>"]
    for idx in sorted(plates):
        parts.append("  <plate>")
        parts.append(f'    <metadata key="index" value="{idx}"/>')
        for fid, used_g, ftype, color in plates[idx]:
            parts.append(f'    <filament id="{fid}" used_g="{used_g}" type="{ftype}" color="{color}"/>')
        parts.append("  </plate>")
    parts.append("</config>")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _zip.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/slice_info.config", "\n".join(parts))
    return path


def _make_archive(archive_id=1, file_path="archives/1/test.3mf", extra_data=None):
    """Create a mock PrintArchive object."""
    archive = MagicMock()
    archive.id = archive_id
    archive.file_path = file_path
    archive.extra_data = extra_data
    return archive


def _make_queue_item(ams_mapping=None, status="printing", plate_id=None):
    """Create a mock PrintQueueItem object."""
    item = MagicMock()
    item.ams_mapping = ams_mapping
    item.status = status
    item.plate_id = plate_id
    return item


def _mock_db_sequential(responses):
    """Create mock db that returns responses in order."""
    db = AsyncMock()
    call_count = [0]

    async def mock_execute(*args, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        result = MagicMock()
        if idx < len(responses):
            result.scalar_one_or_none.return_value = responses[idx]
        else:
            result.scalar_one_or_none.return_value = None
        # For cost aggregation queries that use .scalar() instead of .scalar_one_or_none()
        result.scalar.return_value = None
        return result

    db.execute = mock_execute
    return db


def _resolve_db_plan(plan):
    """Build the tracker's sequential query answers from a declarative plan.

    Each entry is one ``scalar_one_or_none`` answer, in the order
    ``_track_from_3mf`` consumes them: ``("archive", id)``, ``("queue",
    ams_mapping)``, ``("assign", spool_id, ams_id, tray_id)``, ``("spool",
    spool_id)``, or None for a query that finds nothing. Returns the answer list
    and the spool rows by id, so a caller can assert on what got charged.
    """
    answers = []
    spools = {}
    for entry in plan:
        if entry is None:
            answers.append(None)
            continue
        kind, *args = entry
        if kind == "archive":
            answers.append(_make_archive(archive_id=args[0]))
        elif kind == "queue":
            answers.append(_make_queue_item(ams_mapping=args[0]))
        elif kind == "assign":
            answers.append(_make_assignment(spool_id=args[0], ams_id=args[1], tray_id=args[2]))
        elif kind == "spool":
            spool = _make_spool(spool_id=args[0])
            spools[args[0]] = spool
            answers.append(spool)
        else:
            raise AssertionError(f"unknown db plan entry: {entry!r}")
    return answers, spools


async def _run_track_from_3mf(*, db_answers, state, filament_usage, handled_trays, extra_patches=(), **kwargs):
    """Drive `_track_from_3mf` over one mocked print.

    Owns the scaffold every caller shares -- the printer-manager stand-in, the
    sequential DB double and the 3MF extraction patch -- so a test body is only
    its inputs and its assertion. ``extra_patches`` names further
    ``utils.threemf_tools`` functions to stub, as (name, patch-kwargs) pairs.
    Callers needing the settings path to resolve must request the
    ``existing_3mf_path`` fixture.
    """
    printer_manager = MagicMock()
    printer_manager.get_status.return_value = state
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "backend.app.utils.threemf_tools.extract_filament_usage_from_3mf",
                return_value=filament_usage,
            )
        )
        for name, patch_kwargs in extra_patches:
            stack.enter_context(patch(f"backend.app.utils.threemf_tools.{name}", **patch_kwargs))
        return await _track_from_3mf(
            printer_id=1,
            handled_trays=handled_trays,
            printer_manager=printer_manager,
            db=_mock_db_sequential(db_answers),
            **kwargs,
        )


# The slicer left no per-layer data, so every weight split falls back to layer
# ratios. Named once: it is the precondition of the whole linear family, not an
# incidental stub.
_NO_LAYER_DATA = (("extract_layer_filament_usage_from_3mf", {"return_value": None}),)


def _assert_charges(results, expected):
    """Every expected charge matched positionally, on the fields the row names."""
    assert len(results) == len(expected)
    for got, want in zip(results, expected, strict=True):
        for field, value in want.items():
            assert got[field] == value, f"{field}: {got[field]!r} != {value!r}"


class TestOnPrintStart:
    """`on_print_start` opens the session that every later charge is attributed to.

    A session is what makes a print's grams chargeable at all, so it is created
    whenever the printer reports an AMS -- even when no tray reports a usable
    remain%, because the 3MF path does not need one. `tray_remain_start` keeps
    only the trays that DID report 0-100: remain = -1 is "no wire fullness
    exists" (doctrine rule 8), not zero.
    """

    @pytest.fixture(autouse=True)
    def _clear_sessions(self):
        _active_sessions.clear()
        yield
        _active_sessions.clear()

    @pytest.mark.parametrize(
        ("ams_data", "tray_now", "expect_session", "expect_remain", "expect_tray_now_at_start"),
        [
            pytest.param(
                [{"id": 0, "tray": [{"id": 0, "remain": 80}]}],
                5,
                True,
                {(0, 0): 80},
                5,
                id="readable_remain_is_captured",
            ),
            pytest.param(
                [{"id": 0, "tray": [{"id": 0, "remain": 80}, {"id": 1, "remain": 50}]}],
                5,
                True,
                {(0, 0): 80, (0, 1): 50},
                5,
                id="every_reporting_tray_is_captured",
            ),
            pytest.param(
                [{"id": 0, "tray": [{"id": 0, "remain": -1}]}],
                255,
                True,
                {},
                255,
                id="unreadable_remain_still_opens_a_session",
            ),
            pytest.param(
                [{"id": 0, "tray": [{"id": 0, "remain": 80}]}],
                9,
                True,
                {(0, 0): 80},
                9,
                id="loaded_tray_at_start_is_recorded",
            ),
            pytest.param(
                [{"id": 0, "tray": [{"id": 0, "remain": 80}]}],
                255,
                True,
                {(0, 0): 80},
                255,
                id="nothing_loaded_at_start_records_255",
            ),
            pytest.param([], 255, False, None, None, id="no_ams_opens_no_session"),
        ],
    )
    @pytest.mark.asyncio
    async def test_session_capture(self, ams_data, tray_now, expect_session, expect_remain, expect_tray_now_at_start):
        pm = _make_printer_manager(_make_printer_state(ams_data, tray_now=tray_now))

        await on_print_start(1, {"subtask_name": "Benchy"}, pm)

        if not expect_session:
            assert 1 not in _active_sessions
            return
        assert 1 in _active_sessions
        session = _active_sessions[1]
        assert session.print_name == "Benchy"
        assert session.tray_remain_start == expect_remain
        assert session.tray_now_at_start == expect_tray_now_at_start


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
        t3_spool = _make_spool(spool_id=8, label_weight=1000, weight_used=0)
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
    async def test_updates_non_bl_spool_from_3mf(self, existing_3mf_path):
        """Non-BL spool gets weight_used from 3MF used_g for completed print."""
        spool = _make_spool(spool_id=5, label_weight=1000, weight_used=100)
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
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
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
    async def test_tracks_bl_spools_via_3mf(self, existing_3mf_path):
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
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
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
    async def test_skips_already_handled_trays(self, existing_3mf_path):
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
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
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
    async def test_slot_to_tray_mapping(self, existing_3mf_path):
        """3MF slot_id maps correctly to (ams_id, tray_id) via tray_now."""
        # tray_now=4 → ams_id=1, tray_id=0 (single filament uses tray_now)
        spool = _make_spool(spool_id=9)
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
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
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
    async def test_updates_ams_auto_tagless_spool_from_3mf(self, existing_3mf_path):
        """An auto-minted tagless spool gets the 3MF Path-1 decrement."""
        spool = _make_spool(spool_id=9, label_weight=1000, weight_used=40, tag_uid=None, tray_uuid=None)
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
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
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

        # The zero-gram guard (scenario C6) runs AFTER both charging lanes and does
        # its own assignment lookup by design — this shape is exactly what it exists
        # to report. Held out here so the query count keeps measuring the lane this
        # test is about, and asserted below so its firing is covered rather than
        # merely tolerated.
        with patch.object(usage_tracker_module, "_warn_zero_gram_tagless_charge", AsyncMock()) as zero_gram_guard:
            results = await on_print_complete(1, {"status": "completed"}, pm, db)

        assert results == []
        db.commit.assert_not_called()
        # Guard fired before any assignment/spool query (only 3MF searches ran).
        assert db.execute.await_count <= 2
        zero_gram_guard.assert_awaited_once(), "a completed print charging 0 g must reach the C6 guard"


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
    async def test_3mf_uses_snapshot_instead_of_live_query(self, existing_3mf_path):
        """_track_from_3mf uses snapshot spool_id without querying SpoolAssignment."""
        spool = _make_spool(spool_id=42, label_weight=1000)
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
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
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
    async def test_3mf_falls_back_to_live_query_without_snapshot(self, existing_3mf_path):
        """_track_from_3mf queries SpoolAssignment when no snapshot exists."""
        spool = _make_spool(spool_id=5, label_weight=1000)
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
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
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
        spool = _make_spool(spool_id=77, label_weight=1000)

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
        spool = _make_spool(spool_id=33, label_weight=1000)
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
    async def test_snapshot_survives_mid_print_unlink(self, existing_3mf_path):
        """Core bug scenario: snapshot provides spool_id after mid-print unlink.

        Simulates the #459 scenario: spool runs empty mid-print, on_ams_change
        deletes the SpoolAssignment, but the snapshot from print start still
        has the spool_id so usage is correctly attributed at print completion.
        """
        spool = _make_spool(spool_id=8, label_weight=1000, weight_used=50)
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
                # _resolve_run_context's durable ARCHIVE plate tier: this session
                # carries plate_id=None, so it asks the archive row for the plate the
                # printer stated at start. None here keeps this test's premise (the
                # plate is unknown) — it is the sequence that changed, not the case.
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),
                # Cost-update block re-selects the archive to mutate cost.
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
            ]
        )

        with (
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
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
    """`_spool_color_to_hex` normalises `Spool.rgba` (RRGGBBAA, no #) to #RRGGBB.

    None is the "leave the 3MF's own colour alone" answer, so anything
    unparseable has to reach it rather than producing a plausible-looking wrong
    colour.
    """

    @pytest.mark.parametrize(
        ("rgba", "expected"),
        [
            pytest.param("000000FF", "#000000", id="alpha_is_dropped"),
            pytest.param("EC984CFF", "#EC984C", id="alpha_is_dropped_from_a_mixed_colour"),
            pytest.param("ec984cff", "#EC984C", id="lowercase_is_uppercased"),
            pytest.param("161616", "#161616", id="a_six_char_value_needs_no_alpha"),
            pytest.param("#000000FF", "#000000", id="a_leading_hash_is_tolerated"),
            pytest.param(None, None, id="a_missing_colour_defers_to_the_3mf"),
            pytest.param("", None, id="an_empty_colour_defers_to_the_3mf"),
            pytest.param("FFF", None, id="too_short_to_be_a_colour_defers_to_the_3mf"),
        ],
    )
    def test_normalisation(self, rgba, expected):
        assert _spool_color_to_hex(rgba) == expected


class TestArchiveColorsFromSpools:
    """`_archive_colors_from_spools` rebuilds an archive's colours from the rolls that fed it.

    All-or-nothing by design: unless EVERY slot that consumed filament resolved
    to a spool with a colour, the answer is None and the 3MF's own colours stay.
    A partial answer would silently drop the unmatched slots from the archive,
    which reads as a plate that used fewer filaments than it did.
    """

    @pytest.mark.parametrize(
        ("usage", "results", "expected"),
        [
            pytest.param(
                [{"slot_id": 1, "used_g": 15.9, "color": "#161616"}],
                [{"slot_id": 1, "color": "#000000"}],
                ["#000000"],
                id="a_matched_slot_supplies_its_spools_colour",
            ),
            pytest.param(
                [
                    {"slot_id": 1, "used_g": 10.0, "color": "#111111"},
                    {"slot_id": 2, "used_g": 20.0, "color": "#222222"},
                ],
                # deliberately out of slot order
                [{"slot_id": 2, "color": "#00FF00"}, {"slot_id": 1, "color": "#FF0000"}],
                ["#FF0000", "#00FF00"],
                id="output_is_slot_ordered_not_result_ordered",
            ),
            pytest.param(
                [
                    {"slot_id": 1, "used_g": 10.0, "color": "#111111"},
                    {"slot_id": 2, "used_g": 20.0, "color": "#222222"},
                ],
                [{"slot_id": 1, "color": "#000000"}, {"slot_id": 2, "color": "#000000"}],
                ["#000000"],
                id="repeated_colours_collapse_to_one_entry",
            ),
            pytest.param(
                [
                    {"slot_id": 1, "used_g": 10.0, "color": "#111111"},
                    {"slot_id": 2, "used_g": 20.0, "color": "#222222"},
                ],
                [{"slot_id": 1, "color": "#000000"}],
                None,
                id="a_partial_match_leaves_the_3mf_colours_alone",
            ),
            pytest.param(
                [{"slot_id": 1, "used_g": 15.0, "color": "#161616"}],
                [{"slot_id": 1, "color": None}],
                None,
                id="a_spool_with_no_colour_is_not_a_match",
            ),
            pytest.param(
                [
                    {"slot_id": 1, "used_g": 15.0, "color": "#161616"},
                    {"slot_id": 2, "used_g": 0.0, "color": "#888888"},
                ],
                [{"slot_id": 1, "color": "#000000"}],
                ["#000000"],
                id="a_slot_that_consumed_nothing_need_not_match",
            ),
            pytest.param([], [], None, id="no_used_slots_yields_no_answer"),
            pytest.param(
                [{"slot_id": 1, "used_g": 15.0, "color": "#161616"}],
                # the remain%-delta fallback carries no slot_id
                [{"slot_id": None, "color": "#000000"}],
                None,
                id="an_ams_fallback_result_cannot_satisfy_a_3mf_slot",
            ),
        ],
    )
    def test_all_or_nothing(self, usage, results, expected):
        assert _archive_colors_from_spools(usage, results) == expected


class TestArchiveFilamentColorRewrite:
    """`_track_from_3mf` overwrites the archive's filament_color with the
    matched inventory spool colour at print completion (#1494)."""

    @pytest.mark.asyncio
    async def test_archive_color_adopts_spool_color(self, existing_3mf_path):
        """A print from a #000000 inventory spool whose 3MF says #161616 ends
        up with the archive showing the spool's #000000."""
        spool = _make_spool(spool_id=5, label_weight=1000, weight_used=100, rgba="000000FF")
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
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
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
    async def test_archive_color_untouched_when_spool_has_no_color(self, existing_3mf_path):
        """A spool with no rgba leaves the 3MF colour in place."""
        spool = _make_spool(spool_id=5, label_weight=1000, weight_used=100, rgba=None)
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
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
        ):
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
    async def test_single_filament_multi_feeder_splits_by_layer_span(self, existing_3mf_path):
        """AMS backup: one colour fed from tray0 then tray1 splits proportionally.

        Reproduces the 006 shape — a spool runs dry mid-print and the AMS auto-
        refills from a sibling tray; each tray's spool must be charged its span,
        not the whole print dumped on the print-start mapped slot.
        """
        spool0 = _make_spool(spool_id=10, label_weight=1000, weight_used=0)
        spool1 = _make_spool(spool_id=20, label_weight=1000, weight_used=0)
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

        with (
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
    async def test_single_feeder_unchanged(self, existing_3mf_path):
        """A one-entry change log is not a split — full weight to the one tray."""
        spool = _make_spool(spool_id=7, label_weight=1000, weight_used=0)
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

        with (
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
    async def test_multi_filament_backup_splits_per_slot(self, existing_3mf_path):
        """Multi-colour print: slot1 backup-switches (tray0→tray3), slot2 steady.

        The old `len(nonzero_slots) == 1` gate skipped the split entirely for any
        print using >=2 filament slots, so slot1's backup roll (tray3) was charged
        nothing and its share was dumped on tray0. Now slot1 splits across its own
        feeders while slot2 charges only its mapped tray — no cross-slot leakage.
        """
        spool0 = _make_spool(spool_id=10, label_weight=1000, weight_used=0)  # slot1 home tray0
        spool3 = _make_spool(spool_id=30, label_weight=1000, weight_used=0)  # slot1 backup tray3
        spool1 = _make_spool(spool_id=20, label_weight=1000, weight_used=0)  # slot2 tray1
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

        with (
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
    async def test_foreign_single_feeder_charges_observed_tray(self, existing_3mf_path):
        """No ams_mapping and no queue item — the feeder resolves from live tray_now."""
        spool = _make_spool(spool_id=9, label_weight=1000, weight_used=0)
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

        with (
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
    async def test_foreign_multi_feeder_splits(self, existing_3mf_path):
        """A foreign single-colour print fed from two trays splits identically —
        the split needs no queue mapping (single active filament)."""
        spool0 = _make_spool(spool_id=11, label_weight=1000, weight_used=0)
        spool1 = _make_spool(spool_id=12, label_weight=1000, weight_used=0)
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

        with (
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
    async def test_foreign_completion_charges_without_farm_unit_mutation(self, existing_3mf_path):
        """End-to-end via on_print_complete: a foreign session (plate_id=None,
        no ams_mapping) charges the spool and adds ONLY SpoolUsageHistory rows —
        never a PrintQueueItem — so farm queue state is untouched."""
        from backend.app.models.print_queue import PrintQueueItem

        spool = _make_spool(spool_id=5, label_weight=1000, weight_used=0)
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
                # _resolve_run_context's durable ARCHIVE plate tier — the foreign
                # session has plate_id=None, so the archive row is asked for the plate
                # the printer stated at print start. None keeps this test foreign and
                # plate-less; only the call sequence changed.
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),  # _track archive
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),  # no queue item
                MagicMock(scalar_one_or_none=MagicMock(return_value=assign)),  # live assignment
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),  # spool
                MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),  # cost re-select
            ]
        )

        pm = _make_printer_manager(_split_state([(0, 0)], tray_now=0, last_loaded_tray=0))
        filament_usage = [{"slot_id": 1, "used_g": 30.0, "type": "PLA", "color": ""}]

        with (
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


class TestOnPrintComplete:
    """Tests for on_print_complete() — path ordering and interaction."""

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
    async def test_bl_spool_uses_3mf(self, existing_3mf_path):
        """BL spool (with tag_uid) is tracked via 3MF, not just AMS delta."""
        spool = _make_spool(spool_id=1, tag_uid="AABB1122", label_weight=1000)
        assignment = _make_assignment(spool_id=1, printer_id=1, ams_id=0, tray_id=0)
        archive = _make_archive(archive_id=10)

        # Setup: session with AMS remain data
        _active_sessions[1] = PrintSession(
            printer_id=1,
            print_name="Benchy",
            started_at=datetime.now(timezone.utc),
            tray_remain_start={(0, 0): 80},
        )

        # Mock printer state: tray_now=0 (AMS0-T0), single filament
        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            raw_data={"ams": [{"id": 0, "tray": [{"id": 0, "remain": 70}]}]},
            progress=100,
            layer_num=50,
            tray_now=0,
        )

        # db returns: guard(archive.started_at, usage-count), then archive,
        # queue_item(None), assignment, spool for the 3MF path.
        # The third None is _resolve_run_context's durable ARCHIVE plate tier: this
        # session carries no plate_id, so the archive row is asked for the plate the
        # printer stated at print start. None keeps the plate unknown, as before —
        # the call sequence grew, the case did not.
        db = _mock_db_sequential([archive, None, None, archive, None, assignment, spool])

        filament_usage = [{"slot_id": 1, "used_g": 15.0, "type": "PLA", "color": "#FF0000"}]

        with (
            patch(
                "backend.app.utils.threemf_tools.extract_filament_usage_from_3mf",
                return_value=filament_usage,
            ),
        ):
            results = await on_print_complete(
                printer_id=1,
                data={"status": "completed"},
                printer_manager=printer_manager,
                db=db,
                archive_id=10,
            )

        # 3MF path should handle it (BL guard removed)
        assert len(results) >= 1
        assert results[0]["spool_id"] == 1
        assert results[0]["weight_used"] == 15.0

    @pytest.mark.asyncio
    async def test_ams_delta_fallback_no_archive(self):
        """AMS delta tracks consumption when archive_id is None."""
        spool = _make_spool(spool_id=2, label_weight=1000)
        assignment = _make_assignment(spool_id=2)

        _active_sessions[1] = PrintSession(
            printer_id=1,
            print_name="Test",
            started_at=datetime.now(timezone.utc),
            tray_remain_start={(0, 0): 80},
        )

        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            raw_data={"ams": [{"id": 0, "tray": [{"id": 0, "remain": 70}]}]},
            tray_now=0,
            last_loaded_tray=-1,
        )

        # Pad 2 Nones for _find_3mf_by_filename DB queries (library + archive search),
        # then assignment and spool for the AMS fallback path
        db = _mock_db_sequential([None, None, assignment, spool])

        results = await on_print_complete(
            printer_id=1,
            data={"status": "completed"},
            printer_manager=printer_manager,
            db=db,
            archive_id=None,
        )

        assert len(results) == 1
        assert results[0]["spool_id"] == 2
        # 10% of 1000g = 100g
        assert results[0]["weight_used"] == 100.0
        assert results[0]["percent_used"] == 10

    @pytest.mark.asyncio
    async def test_no_double_tracking(self, existing_3mf_path):
        """When 3MF handles a tray, AMS delta skips it."""
        spool = _make_spool(spool_id=1, label_weight=1000)
        assignment = _make_assignment(spool_id=1)
        archive = _make_archive(archive_id=10)

        _active_sessions[1] = PrintSession(
            printer_id=1,
            print_name="Benchy",
            started_at=datetime.now(timezone.utc),
            tray_remain_start={(0, 0): 80},
        )

        # tray_now=0 matches the single filament slot
        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            raw_data={"ams": [{"id": 0, "tray": [{"id": 0, "remain": 70}]}]},
            progress=100,
            layer_num=50,
            tray_now=0,
        )

        # db returns: guard(archive.started_at, usage-count), then archive,
        # queue_item(None), assignment, spool for the 3MF path.
        # The third None is _resolve_run_context's durable ARCHIVE plate tier: this
        # session carries no plate_id, so the archive row is asked for the plate the
        # printer stated at print start. None keeps the plate unknown, as before —
        # the call sequence grew, the case did not.
        db = _mock_db_sequential([archive, None, None, archive, None, assignment, spool])

        filament_usage = [{"slot_id": 1, "used_g": 15.0, "type": "PLA", "color": "#FF0000"}]

        with (
            patch(
                "backend.app.utils.threemf_tools.extract_filament_usage_from_3mf",
                return_value=filament_usage,
            ),
        ):
            results = await on_print_complete(
                printer_id=1,
                data={"status": "completed"},
                printer_manager=printer_manager,
                db=db,
                archive_id=10,
            )

        # Only 1 result (3MF), NOT 2 (3MF + AMS delta)
        assert len(results) == 1
        assert results[0]["weight_used"] == 15.0


class TestTrackFrom3mfSlotResolution:
    """Which feeder a 3MF slot is charged to -- the slot -> (ams_id, tray_id) ladder.

    A 3MF names filament SLOTS; the ledger charges AMS TRAYS, and nothing in the
    file says which is which. The tracker resolves that in a fixed order of
    decreasing authority: the mapping the print command carried, then the queue
    item's mapping, then the tray the printer reports (only when a single
    filament makes it unambiguous), then the tray it reported at print start,
    then the last tray it loaded. Getting this order wrong charges the grams to
    a roll that never fed -- the mis-attribution class doctrine rule 4 exists to
    catch -- so every rung has a row here and the row names the rung.
    """

    @pytest.mark.parametrize(
        "case",
        [
            pytest.param(
                {
                    "db": [("archive", 10), None, ("assign", 2, 1, 3), ("spool", 2)],
                    "state": {"progress": 100, "layer_num": 50, "tray_now": 7},
                    # slot_id 12 would default-map to AMS2-T3; tray_now says AMS1-T3
                    "usage": [{"slot_id": 12, "used_g": 10.6, "type": "PLA", "color": "#FF0000"}],
                    "kwargs": {"archive_id": 10},
                    "expect": [{"spool_id": 2, "ams_id": 1, "tray_id": 3, "weight_used": 10.6}],
                    "handled": {(1, 3)},
                },
                id="tray_now_outranks_slot_id_default_when_single_filament",
            ),
            pytest.param(
                {
                    "db": [("archive", 10), None, ("assign", 1, 0, 0), ("spool", 1), None],
                    # tray_now names ONE tray, which cannot speak for two filaments
                    "state": {"progress": 100, "layer_num": 50, "tray_now": 4},
                    "usage": [
                        {"slot_id": 1, "used_g": 10.0, "type": "PLA", "color": ""},
                        {"slot_id": 2, "used_g": 5.0, "type": "PETG", "color": ""},
                    ],
                    "kwargs": {"archive_id": 10},
                    "expect": [{"ams_id": 0, "tray_id": 0}],
                },
                id="tray_now_is_refused_for_multi_filament_and_default_mapping_stands",
            ),
            pytest.param(
                {
                    "db": [("archive", 20), ("queue", "[7, -1, -1, -1]"), ("assign", 5, 1, 3), ("spool", 5)],
                    "state": {"progress": 100, "layer_num": 50, "tray_now": 7},
                    "usage": [{"slot_id": 1, "used_g": 25.0, "type": "PETG", "color": ""}],
                    "kwargs": {"archive_id": 20},
                    "expect": [{"spool_id": 5, "ams_id": 1, "tray_id": 3, "weight_used": 25.0}],
                },
                id="queue_mapping_outranks_slot_id_default",
            ),
            pytest.param(
                {
                    "db": [
                        ("archive", 30),
                        ("queue", "[0, 6]"),
                        ("assign", 1, 0, 0),
                        ("spool", 1),
                        ("assign", 2, 1, 2),
                        ("spool", 2),
                    ],
                    "state": {"progress": 100, "layer_num": 50, "tray_now": 6},
                    "usage": [
                        {"slot_id": 1, "used_g": 10.0, "type": "PLA", "color": ""},
                        {"slot_id": 2, "used_g": 5.0, "type": "PETG", "color": ""},
                    ],
                    "kwargs": {"archive_id": 30},
                    "expect": [
                        {"spool_id": 1, "ams_id": 0, "tray_id": 0, "weight_used": 10.0},
                        {"spool_id": 2, "ams_id": 1, "tray_id": 2, "weight_used": 5.0},
                    ],
                },
                id="queue_mapping_charges_every_slot_of_a_multi_filament_plate",
            ),
            pytest.param(
                {
                    # no queue answer in the plan: a print-command mapping skips that query
                    "db": [("archive", 50), ("assign", 10, 2, 1), ("spool", 10)],
                    "state": {
                        "progress": 100,
                        "layer_num": 50,
                        "tray_now": 0,
                        "last_loaded_tray": 0,
                    },
                    "usage": [{"slot_id": 2, "used_g": 1.57, "type": "PLA", "color": "#FFFFFF"}],
                    "kwargs": {"archive_id": 50, "ams_mapping": [-1, 9]},
                    # 1.57 g is rounded to the ledger's one decimal
                    "expect": [{"spool_id": 10, "ams_id": 2, "tray_id": 1, "weight_used": 1.6}],
                },
                id="print_command_mapping_outranks_queue_and_tray_now",
            ),
            pytest.param(
                {
                    "db": [("archive", 70), None, ("assign", 3, 1, 1), ("spool", 3)],
                    "state": {
                        "progress": 100,
                        "layer_num": 50,
                        "tray_now": 255,
                        "last_loaded_tray": 9,
                    },
                    "usage": [{"slot_id": 1, "used_g": 5.0, "type": "PLA", "color": ""}],
                    "kwargs": {"archive_id": 70, "tray_now_at_start": 5},
                    "expect": [{"ams_id": 1, "tray_id": 1}],
                },
                id="tray_now_at_start_outranks_last_loaded_tray",
            ),
            pytest.param(
                {
                    "db": [("archive", 60), None, ("assign", 11, 2, 1), ("spool", 11)],
                    # H2D shape: nothing loaded now and nothing loaded at start
                    "state": {
                        "progress": 100,
                        "layer_num": 50,
                        "tray_now": 255,
                        "last_loaded_tray": 9,
                    },
                    "usage": [{"slot_id": 6, "used_g": 1.52, "type": "PLA", "color": "#7CC4D5"}],
                    "kwargs": {"archive_id": 60, "tray_now_at_start": 255},
                    "expect": [{"spool_id": 11, "ams_id": 2, "tray_id": 1}],
                },
                id="last_loaded_tray_is_the_final_fallback",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_slot_resolution_ladder(self, case, existing_3mf_path):
        handled: set[tuple[int, int]] = set()
        answers, _ = _resolve_db_plan(case["db"])

        results = await _run_track_from_3mf(
            db_answers=answers,
            state=SimpleNamespace(**case["state"]),
            filament_usage=case["usage"],
            handled_trays=handled,
            status="completed",
            print_name="Test",
            **case["kwargs"],
        )

        _assert_charges(results, case["expect"])
        assert case.get("handled", set()) <= handled


class TestTrackFrom3mfScaling:
    """How many of the 3MF's estimated grams a print is charged.

    A completed print consumed the whole estimate. A print that stopped early
    consumed part of it, and the tracker prefers the G-code's own cumulative
    figure at the layer it reached, falling back to linear scaling by progress
    when the slicer left no per-layer data. Over-charging a partial print is how
    a roll's ledger drifts away from the grams that physically left it.
    """

    @pytest.mark.parametrize(
        "case",
        [
            pytest.param(
                {
                    "state": {"progress": 100, "layer_num": 50, "tray_now": 0},
                    "status": "completed",
                    "used_g": 20.0,
                    "extra_patches": (),
                    "expect_weight": 20.0,
                },
                id="completed_print_charges_the_full_estimate",
            ),
            pytest.param(
                {
                    "state": {"progress": 50, "layer_num": 25, "tray_now": 0},
                    "status": "failed",
                    "used_g": 20.0,
                    "extra_patches": _NO_LAYER_DATA,
                    "expect_weight": 10.0,
                    "expect_handled": {(0, 0)},
                },
                id="partial_print_without_layer_data_falls_back_to_linear",
            ),
            pytest.param(
                {
                    "state": {"progress": 50, "layer_num": 25, "tray_now": 0},
                    "status": "failed",
                    "used_g": 100.0,
                    "extra_patches": _NO_LAYER_DATA,
                    "expect_weight": 50.0,
                    # the charge lands on the spool row, not just the result
                    "expect_spool_charge": (1, 50.0),
                },
                id="linear_fallback_charges_the_spool_row_itself",
            ),
            pytest.param(
                {
                    "state": {"progress": 50, "layer_num": 25, "tray_now": 0},
                    "status": "failed",
                    "used_g": 20.0,
                    "extra_patches": (
                        (
                            "extract_layer_filament_usage_from_3mf",
                            {"return_value": {10: {0: 2000.0}, 25: {0: 5000.0}, 50: {0: 10000.0}}},
                        ),
                        ("get_cumulative_usage_at_layer", {"return_value": {0: 5000.0}}),
                        (
                            "extract_filament_properties_from_3mf",
                            {"return_value": {1: {"density": 1.24, "diameter": 1.75}}},
                        ),
                        ("mm_to_grams", {"return_value": 12.0}),
                    ),
                    # the G-code's 12.0 g at layer 25, NOT linear's 10.0 g
                    "expect_weight": 12.0,
                },
                id="partial_print_with_layer_data_uses_the_gcode_cumulative",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_scaling_ladder(self, case, existing_3mf_path):
        handled: set[tuple[int, int]] = set()
        answers, spools = _resolve_db_plan([("archive", 10), None, ("assign", 1, 0, 0), ("spool", 1)])

        results = await _run_track_from_3mf(
            db_answers=answers,
            state=SimpleNamespace(**case["state"]),
            filament_usage=[{"slot_id": 1, "used_g": case["used_g"], "type": "PLA", "color": ""}],
            handled_trays=handled,
            extra_patches=case["extra_patches"],
            archive_id=10,
            status=case["status"],
            print_name="Benchy",
        )

        assert len(results) == 1
        assert results[0]["weight_used"] == case["expect_weight"]
        assert case.get("expect_handled", set()) <= handled
        if "expect_spool_charge" in case:
            spool_id, charged = case["expect_spool_charge"]
            assert spools[spool_id].weight_used == charged


class TestTrackFrom3mfBindingAdjudication:
    """WHICH spool row a tray's grams are charged to when the binding moved.

    The print-start snapshot records where every roll was when the print began.
    A binding created AFTER that moment means a human swapped the roll mid-print,
    so the grams belong to the roll that was actually feeding; a binding that
    predates the print is the same tenancy the snapshot already describes, and
    the snapshot wins. This is doctrine rule 9 -- an assignment claims WHERE a
    roll is -- read at the charging boundary, and it stays two separate tests
    because the two directions are opposite verdicts on the same evidence.
    """

    @pytest.mark.asyncio
    async def test_live_binding_created_mid_print_supersedes_the_snapshot(self, existing_3mf_path):
        started_at = datetime.now(timezone.utc)
        answers, _ = _resolve_db_plan([("archive", 80), None, ("assign", 2, 0, 0), ("spool", 2)])
        answers[2].created_at = started_at + timedelta(seconds=5)

        results = await _run_track_from_3mf(
            db_answers=answers,
            state=SimpleNamespace(progress=100, layer_num=50, tray_now=0),
            filament_usage=[{"slot_id": 1, "used_g": 10.0, "type": "PLA", "color": ""}],
            handled_trays=set(),
            archive_id=80,
            status="completed",
            print_name="MidPrintReassign",
            spool_assignments={(0, 0): 1},
            print_started_at=started_at,
        )

        _assert_charges(results, [{"spool_id": 2}])

    @pytest.mark.asyncio
    async def test_live_binding_predating_the_print_leaves_the_snapshot_standing(self, existing_3mf_path):
        started_at = datetime.now(timezone.utc)
        # the live query answers with spool 2's binding, but it predates the print,
        # so the snapshot's spool 1 is the row that fed
        answers, _ = _resolve_db_plan([("archive", 81), None, ("assign", 2, 0, 0), ("spool", 1)])
        answers[2].created_at = started_at - timedelta(seconds=5)

        results = await _run_track_from_3mf(
            db_answers=answers,
            state=SimpleNamespace(progress=100, layer_num=50, tray_now=0),
            filament_usage=[{"slot_id": 1, "used_g": 10.0, "type": "PLA", "color": ""}],
            handled_trays=set(),
            archive_id=81,
            status="completed",
            print_name="SnapshotPreserved",
            spool_assignments={(0, 0): 1},
            print_started_at=started_at,
        )

        _assert_charges(results, [{"spool_id": 1}])


class TestTrayChangeSplit:
    """A print that fed from more than one tray is charged per SEGMENT.

    When the AMS auto-refills from a backup slot mid-print, one print consumes
    two or more rolls, and `tray_change_log` -- [(tray, layer), ...] -- is the
    only record of where each handover fell. Doctrine rule 4: every feeder that
    consumed must be charged, so each segment becomes its own charge against the
    roll that was feeding for it.

    Two firmware facts shape the fallback ladder. The exact split needs the
    G-code's own cumulative figure at the handover layer; without it the split is
    the segment's LAYER RATIO. And a P1S resets `total_layer_num` to 0 at print
    end, so the denominator cascades total_layers -> last_layer_num (captured
    before the reset) -> an equal split across segments. The equal split is
    still wrong, but it is BOUNDED, which is the point of having it: an absent
    denominator must not concentrate the whole print onto one roll.
    """

    @pytest.mark.parametrize(
        "case",
        [
            pytest.param(
                {
                    "db": [
                        ("archive", 101),
                        None,
                        ("assign", 10, 0, 2),
                        ("spool", 10),
                        ("assign", 20, 0, 1),
                        ("spool", 20),
                    ],
                    "state": {
                        "tray_now": 1,
                        "last_loaded_tray": 1,
                        "total_layers": 100,
                        "tray_change_log": [(2, 0), (1, 40)],
                    },
                    "used_g": 50.0,
                    "kwargs": {"archive_id": 101},
                    # tray 2 fed layers 0-40 of 100; the last segment takes the remainder
                    "expect": [
                        {"ams_id": 0, "tray_id": 2, "weight_used": 20.0},
                        {"ams_id": 0, "tray_id": 1, "weight_used": 30.0},
                    ],
                },
                id="two_segments_split_by_layer_ratio",
            ),
            pytest.param(
                {
                    "db": [
                        ("archive", 105),
                        None,
                        ("assign", 1, 0, 0),
                        ("spool", 1),
                        ("assign", 2, 0, 1),
                        ("spool", 2),
                        ("assign", 3, 0, 2),
                        ("spool", 3),
                    ],
                    "state": {
                        "tray_now": 2,
                        "last_loaded_tray": 2,
                        "total_layers": 100,
                        "tray_change_log": [(0, 0), (1, 30), (2, 70)],
                    },
                    "used_g": 100.0,
                    "kwargs": {"archive_id": 105},
                    "expect": [
                        {"ams_id": 0, "tray_id": 0, "weight_used": 30.0},
                        {"ams_id": 0, "tray_id": 1, "weight_used": 40.0},
                        {"ams_id": 0, "tray_id": 2, "weight_used": 30.0},
                    ],
                },
                id="three_segments_each_charged_their_own_span",
            ),
            pytest.param(
                {
                    # a print-command mapping skips the queue query, so no queue answer
                    "db": [("archive", 200), ("assign", 10, 0, 0), ("spool", 10), ("assign", 20, 0, 1), ("spool", 20)],
                    "state": {
                        "tray_now": 1,
                        "last_loaded_tray": 1,
                        "total_layers": 100,
                        "tray_change_log": [(0, 0), (1, 30)],
                    },
                    "used_g": 78.0,
                    # the slicer mapping named tray 0; the printer then swapped away from it
                    "kwargs": {"archive_id": 200, "ams_mapping": [0]},
                    "expect_total": 78.0,
                    "handled": {(0, 0), (0, 1)},
                },
                id="observed_switch_outranks_the_stale_print_command_mapping",
            ),
            pytest.param(
                {
                    "db": [("archive", 104), None, None, ("assign", 20, 0, 3), ("spool", 20)],
                    "state": {
                        "tray_now": 3,
                        "last_loaded_tray": 3,
                        "total_layers": 100,
                        "tray_change_log": [(5, 0), (3, 50)],
                    },
                    "used_g": 40.0,
                    "kwargs": {"archive_id": 104},
                    # tray 5 holds no bound roll, so its grams have nobody to charge
                    "expect": [{"ams_id": 0, "tray_id": 3, "spool_id": 20}],
                },
                id="a_segment_with_no_bound_roll_is_skipped_not_reassigned",
            ),
            pytest.param(
                {
                    "db": [
                        ("archive", 171),
                        None,
                        ("assign", 10, 0, 0),
                        ("spool", 10),
                        ("assign", 20, 0, 1),
                        ("spool", 20),
                    ],
                    # P1S shape: the firmware zeroed both layer counters at print end
                    "state": {
                        "layer_num": 0,
                        "tray_now": 1,
                        "last_loaded_tray": 1,
                        "total_layers": 0,
                        "tray_change_log": [(0, 0), (1, 180)],
                    },
                    "used_g": 260.0,
                    "kwargs": {"archive_id": 171, "last_layer_num": 260},
                    "expect": [
                        {"ams_id": 0, "tray_id": 0, "weight_used": 180.0},
                        {"ams_id": 0, "tray_id": 1, "weight_used": 80.0},
                    ],
                },
                id="last_layer_num_substitutes_for_a_reset_total_layers",
            ),
            pytest.param(
                {
                    "db": [
                        ("archive", 172),
                        None,
                        ("assign", 10, 0, 0),
                        ("spool", 10),
                        ("assign", 20, 0, 1),
                        ("spool", 20),
                    ],
                    "state": {
                        "layer_num": 0,
                        "tray_now": 1,
                        "last_loaded_tray": 1,
                        "total_layers": 0,
                        "tray_change_log": [(0, 0), (1, 50)],
                    },
                    "used_g": 60.0,
                    # no denominator survives from either source
                    "kwargs": {"archive_id": 172, "last_layer_num": 0},
                    "expect": [{"weight_used": 30.0}, {"weight_used": 30.0}],
                },
                id="no_denominator_at_all_splits_equally_rather_than_concentrating",
            ),
            pytest.param(
                {
                    "db": [("archive", 102), None, ("assign", 1, 0, 2), ("spool", 1)],
                    "state": {"tray_now": 2, "last_loaded_tray": 2, "total_layers": 100, "tray_change_log": [(2, 0)]},
                    "used_g": 15.0,
                    "kwargs": {"archive_id": 102, "tray_now_at_start": 2},
                    "no_layer_stub": True,
                    "expect": [{"ams_id": 0, "tray_id": 2, "weight_used": 15.0}],
                },
                id="one_log_entry_is_no_switch_and_charges_one_tray",
            ),
            pytest.param(
                {
                    "db": [("archive", 103), None, ("assign", 1, 0, 0), ("spool", 1)],
                    # an empty log is a restart mid-print, not evidence of a switch
                    "state": {"tray_now": 0, "last_loaded_tray": 0, "total_layers": 100, "tray_change_log": []},
                    "used_g": 10.0,
                    "kwargs": {"archive_id": 103, "tray_now_at_start": 0},
                    "no_layer_stub": True,
                    "expect": [{"weight_used": 10.0}],
                },
                id="an_empty_log_charges_one_tray_rather_than_splitting",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_segment_charging(self, case, existing_3mf_path):
        handled: set[tuple[int, int]] = set()
        answers, _ = _resolve_db_plan(case["db"])
        state = {"progress": 100, "layer_num": 100}
        state.update(case["state"])

        results = await _run_track_from_3mf(
            db_answers=answers,
            state=SimpleNamespace(**state),
            filament_usage=[{"slot_id": 1, "used_g": case["used_g"], "type": "PLA", "color": ""}],
            handled_trays=handled,
            extra_patches=() if case.get("no_layer_stub") else _NO_LAYER_DATA,
            status="completed",
            print_name="Segment split",
            **case["kwargs"],
        )

        if "expect_total" in case:
            assert len(results) == 2
            assert sum(r["weight_used"] for r in results) == pytest.approx(case["expect_total"], abs=0.1)
        else:
            _assert_charges(results, case["expect"])
        assert case.get("handled", set()) <= handled

    @pytest.mark.asyncio
    async def test_gcode_cumulative_places_the_handover_exactly(self, existing_3mf_path):
        """With per-layer G-code the boundary is measured, not interpolated.

        The first segment is charged what the G-code says had been extruded by
        the handover layer, and the last segment takes the remainder, so the two
        charges still sum to the 3MF estimate. Kept out of the table because it
        is the only path that reads the G-code, and it needs the layer -> length
        lookups stubbed as functions rather than fixed answers.
        """
        handled: set[tuple[int, int]] = set()
        answers, _ = _resolve_db_plan(
            [("archive", 100), None, ("assign", 10, 0, 1), ("spool", 10), ("assign", 20, 0, 0), ("spool", 20)]
        )

        results = await _run_track_from_3mf(
            db_answers=answers,
            state=SimpleNamespace(
                progress=100,
                layer_num=100,
                tray_now=0,
                last_loaded_tray=0,
                total_layers=100,
                tray_change_log=[(1, 0), (0, 60)],
            ),
            filament_usage=[{"slot_id": 1, "used_g": 30.0, "type": "PLA", "color": ""}],
            handled_trays=handled,
            extra_patches=(
                (
                    "extract_layer_filament_usage_from_3mf",
                    {"return_value": {30: {0: 3000.0}, 60: {0: 6000.0}, 100: {0: 10000.0}}},
                ),
                (
                    "get_cumulative_usage_at_layer",
                    {"side_effect": lambda data, layer: {0: {0: 0.0, 60: 6000.0, 100: 10000.0}.get(layer, 0.0)}},
                ),
                (
                    "extract_filament_properties_from_3mf",
                    {"return_value": {1: {"density": 1.24, "diameter": 1.75}}},
                ),
                ("mm_to_grams", {"side_effect": lambda mm, d, dens: round(mm * 0.003, 1)}),
            ),
            archive_id=100,
            status="completed",
            print_name="Runout Test",
        )

        _assert_charges(
            results,
            [
                # 6000 mm extruded by layer 60
                {"ams_id": 0, "tray_id": 1, "spool_id": 10, "weight_used": 18.0},
                # the remainder of the 30 g estimate
                {"ams_id": 0, "tray_id": 0, "spool_id": 20, "weight_used": 12.0},
            ],
        )
        assert {(0, 1), (0, 0)} <= handled


class TestDecodeMqttMapping:
    """`_decode_mqtt_mapping` turns the wire's snow-encoded slot words into global tray ids.

    The firmware encodes a feeder as ``ams_hw_id * 256 + slot``, and 65535 means
    "this filament slot is not mapped to any tray". Every unmapped entry has to
    survive as -1 rather than collapsing the list, because the caller reads the
    result POSITIONALLY against the 3MF's filament slots.
    """

    @pytest.mark.parametrize(
        ("wire", "expected"),
        [
            pytest.param(None, None, id="no_mapping_on_the_wire"),
            pytest.param([], None, id="empty_mapping_is_no_mapping"),
            pytest.param([65535, 65535, 65535], None, id="every_slot_unmapped_is_no_mapping"),
            pytest.param([0, 1, 2, 3], [0, 1, 2, 3], id="ams0_slots_are_their_own_global_ids"),
            pytest.param([256, 257], [4, 5], id="ams1_slots_offset_by_four"),
            pytest.param([32768], [128], id="ams_ht_keeps_its_hardware_id"),
            # 254 * 256 + 0
            pytest.param([65024], [254], id="external_spool_decodes_to_254"),
            pytest.param([1, 65535, 0], [1, -1, 0], id="an_unmapped_entry_holds_its_position_as_minus_one"),
            pytest.param(
                [1, 0, 65535, 65535, 65535, 65535, 32768],
                [1, 0, -1, -1, -1, -1, 128],
                id="a_real_h2c_mapping_decodes_whole",
            ),
            pytest.param(["foo", 0], [-1, 0], id="a_non_integer_entry_counts_as_unmapped"),
        ],
    )
    def test_decode(self, wire, expected):
        assert _decode_mqtt_mapping(wire) == expected


def _color_ams(trays):
    """AMS data from (ams_id, tray_id, colour, tray_type) tuples."""
    units: dict[int, list] = {}
    for ams_id, tray_id, color, tray_type in trays:
        units.setdefault(ams_id, []).append({"id": tray_id, "tray_color": color, "tray_type": tray_type})
    return [{"id": aid, "tray": t} for aid, t in units.items()]


def _color_usage(slots):
    """3MF filament_usage from (slot_id, colour) tuples."""
    return [{"slot_id": sid, "used_g": 10.0, "type": "PLA", "color": color} for sid, color in slots]


class TestMatchSlotsByColor:
    """`_match_slots_by_color` recovers a slot -> tray mapping from colour alone.

    This is the last resort when no mapping was recorded, so it must answer only
    when the answer is UNIQUE: it returns a mapping when every used 3MF slot
    matches exactly one still-unclaimed tray, and None the moment anything is
    ambiguous. A guessed mapping here charges the grams to whichever roll happens
    to share a colour, which is the mis-attribution doctrine rule 4 guards.
    Colours arrive in two dialects -- the AMS reports RRGGBBAA, the 3MF #RRGGBB
    -- and both normalise to one form before comparison.
    """

    @pytest.mark.parametrize(
        ("usage", "ams", "expected"),
        [
            pytest.param(None, None, None, id="no_usage_and_no_ams"),
            pytest.param([], None, None, id="empty_usage_and_no_ams"),
            pytest.param(None, {"ams": []}, None, id="no_usage_against_an_empty_ams"),
            pytest.param(_color_usage([(1, "#FF0000")]), {"ams": []}, None, id="an_empty_ams_matches_nothing"),
            pytest.param(
                _color_usage([(1, "#FF0000")]),
                {"ams": _color_ams([(0, 0, "FF0000FF", "PLA")])},
                [0],
                id="one_slot_matches_the_one_tray_of_its_colour",
            ),
            pytest.param(
                _color_usage([(1, "#FF0000"), (2, "#00FF00"), (3, "#0000FF")]),
                {"ams": _color_ams([(0, 0, "FF0000FF", "PLA"), (0, 1, "00FF00FF", "PLA"), (0, 2, "0000FFFF", "PLA")])},
                [0, 1, 2],
                id="three_distinct_colours_match_three_trays",
            ),
            pytest.param(
                _color_usage([(1, "#CC0000"), (2, "#00CC00")]),
                {
                    "ams": _color_ams(
                        [
                            (0, 0, "AAAAAAFF", "PLA"),
                            (0, 1, "BBBBBBFF", "PLA"),
                            (1, 0, "CC0000FF", "PETG"),
                            (1, 1, "00CC00FF", "PETG"),
                        ]
                    )
                },
                [4, 5],
                id="a_second_ams_units_trays_report_global_ids",
            ),
            pytest.param(
                _color_usage([(1, "#FF0000"), (2, "#0000FF")]),
                {"ams": _color_ams([(0, 0, "FF0000FF", "PLA"), (128, 0, "0000FFFF", "PLA")])},
                [0, 128],
                id="an_ams_ht_tray_uses_its_raw_ams_id",
            ),
            pytest.param(
                _color_usage([(1, "#FF0000")]),
                {"ams": _color_ams([(0, 0, "FF0000FF", "PLA"), (0, 1, "FF0000FF", "PLA")])},
                None,
                id="two_trays_of_the_same_colour_are_ambiguous",
            ),
            pytest.param(
                _color_usage([(1, "#FF0000")]),
                {"ams": _color_ams([(0, 0, "00FF00FF", "PLA")])},
                None,
                id="a_colour_no_tray_carries_matches_nothing",
            ),
            pytest.param(
                _color_usage([(1, "#AABBCC")]),
                {"ams": _color_ams([(0, 0, "AABBCC80", "PLA")])},
                [0],
                id="the_two_colour_dialects_normalise_to_one_form",
            ),
            pytest.param(
                _color_usage([(1, "#AAbbCC")]),
                {"ams": _color_ams([(0, 0, "aaBBccFF", "PLA")])},
                [0],
                id="colour_matching_ignores_case",
            ),
            pytest.param(
                _color_usage([(1, "#FF0000")]),
                {"ams": _color_ams([(0, 0, "", "PLA"), (0, 1, "FF0000FF", "PLA")])},
                [1],
                id="a_tray_reporting_no_colour_is_skipped",
            ),
            pytest.param(
                _color_usage([(1, "#FF0000")]),
                {"ams": _color_ams([(0, 0, "FF0000FF", ""), (0, 1, "FF0000FF", "PLA")])},
                [1],
                id="an_unloaded_tray_is_skipped_even_when_its_colour_fits",
            ),
            pytest.param(
                [{"slot_id": 1, "used_g": 10.0, "type": "PLA", "color": "#FFF"}],
                {"ams": _color_ams([(0, 0, "FF0000FF", "PLA")])},
                None,
                id="a_3mf_colour_shorter_than_six_chars_cannot_match",
            ),
            pytest.param(
                [{"slot_id": 0, "used_g": 10.0, "type": "PLA", "color": "#FF0000"}],
                {"ams": _color_ams([(0, 0, "FF0000FF", "PLA")])},
                None,
                id="slot_id_zero_is_skipped",
            ),
            pytest.param(
                _color_usage([(1, "#FF0000")]),
                [{"id": 0, "tray": [{"id": 0, "tray_color": "FF0000FF", "tray_type": "PLA"}]}],
                [0],
                id="ams_data_may_arrive_as_a_bare_list",
            ),
            pytest.param(
                _color_usage([(1, "#FF0000"), (2, "#FF0000")]),
                {"ams": _color_ams([(0, 0, "FF0000FF", "PLA"), (0, 1, "FF0000FF", "PLA")])},
                None,
                id="two_slots_wanting_one_colour_stay_ambiguous",
            ),
            pytest.param(
                _color_usage([(1, "#00FF00")]),
                {"ams": [{"id": 0, "tray": [{"id": 0, "tray_color": "00FF00FF", "tray_type": "PLA"}]}]},
                [0],
                id="ams_data_may_arrive_wrapped_under_an_ams_key",
            ),
        ],
    )
    def test_colour_matching(self, usage, ams, expected):
        assert _match_slots_by_color(usage, ams) == expected


class TestMqttMappingIntegration:
    """Integration tests: MQTT mapping field used in _track_from_3mf."""

    @pytest.mark.asyncio
    async def test_h2c_multi_filament_uses_mqtt_mapping(self, existing_3mf_path):
        """H2C: 3 filaments resolved via MQTT mapping field (no ams_mapping, no queue)."""
        # AMS0-T1 (White PLA), AMS0-T0 (Black PLA), AMS128-T0 (Red PLA)
        spool_white = _make_spool(spool_id=1, label_weight=1000)
        spool_black = _make_spool(spool_id=2, label_weight=1000)
        spool_red = _make_spool(spool_id=3, label_weight=1000)
        assign_white = _make_assignment(spool_id=1, ams_id=0, tray_id=1)
        assign_black = _make_assignment(spool_id=2, ams_id=0, tray_id=0)
        assign_red = _make_assignment(spool_id=3, ams_id=128, tray_id=0)
        archive = _make_archive(archive_id=12)

        # db: archive, then 3 pairs of (assignment, spool)
        # No queue lookup because MQTT mapping is found first
        db = _mock_db_sequential(
            [
                archive,
                assign_white,
                spool_white,
                assign_black,
                spool_black,
                assign_red,
                spool_red,
            ]
        )

        # MQTT mapping: slot0→AMS0-T1(1), slot1→AMS0-T0(0), slots2-5→unmapped, slot6→AMS128-T0(32768)
        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            raw_data={"mapping": [1, 0, 65535, 65535, 65535, 65535, 32768]},
            progress=100,
            layer_num=50,
            tray_now=255,
        )

        # 3MF slots 1, 2, 7 (1-based) → indices 0, 1, 6 in mapping
        filament_usage = [
            {"slot_id": 1, "used_g": 21.16, "type": "PLA", "color": "#FFFFFF"},
            {"slot_id": 2, "used_g": 24.22, "type": "PLA", "color": "#000000"},
            {"slot_id": 7, "used_g": 18.47, "type": "PLA", "color": "#F72323"},
        ]
        handled_trays: set[tuple[int, int]] = set()

        with (
            patch(
                "backend.app.utils.threemf_tools.extract_filament_usage_from_3mf",
                return_value=filament_usage,
            ),
        ):
            results = await _track_from_3mf(
                printer_id=1,
                archive_id=12,
                status="completed",
                print_name="Cube + Cube + Cube",
                handled_trays=handled_trays,
                printer_manager=printer_manager,
                db=db,
            )

        assert len(results) == 3

        # slot_id=1 → mapping[0]=1 → AMS0-T1 (White PLA)
        assert results[0]["spool_id"] == 1
        assert results[0]["ams_id"] == 0
        assert results[0]["tray_id"] == 1
        assert results[0]["weight_used"] == 21.2

        # slot_id=2 → mapping[1]=0 → AMS0-T0 (Black PLA)
        assert results[1]["spool_id"] == 2
        assert results[1]["ams_id"] == 0
        assert results[1]["tray_id"] == 0
        assert results[1]["weight_used"] == 24.2

        # slot_id=7 → mapping[6]=32768 → AMS128-T0 (Red PLA)
        assert results[2]["spool_id"] == 3
        assert results[2]["ams_id"] == 128
        assert results[2]["tray_id"] == 0
        assert results[2]["weight_used"] == 18.5

    @pytest.mark.asyncio
    async def test_print_cmd_mapping_takes_priority_over_mqtt(self, existing_3mf_path):
        """ams_mapping from print command is used even when MQTT mapping exists."""
        spool = _make_spool(spool_id=1, label_weight=1000)
        assignment = _make_assignment(spool_id=1, ams_id=0, tray_id=2)
        archive = _make_archive(archive_id=10)

        # db: archive, assignment, spool (no queue lookup when ams_mapping provided)
        db = _mock_db_sequential([archive, assignment, spool])

        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            raw_data={"mapping": [0, 65535]},  # MQTT says slot 0 → AMS0-T0
            progress=100,
            layer_num=50,
            tray_now=255,
        )

        filament_usage = [{"slot_id": 1, "used_g": 10.0, "type": "PLA", "color": ""}]
        handled_trays: set[tuple[int, int]] = set()

        with (
            patch(
                "backend.app.utils.threemf_tools.extract_filament_usage_from_3mf",
                return_value=filament_usage,
            ),
        ):
            results = await _track_from_3mf(
                printer_id=1,
                archive_id=10,
                status="completed",
                print_name="Test",
                handled_trays=handled_trays,
                printer_manager=printer_manager,
                db=db,
                ams_mapping=[2],  # Print cmd says slot 0 → AMS0-T2 (overrides MQTT)
            )

        assert len(results) == 1
        assert results[0]["ams_id"] == 0
        assert results[0]["tray_id"] == 2  # From print_cmd mapping, not MQTT


class TestPositionBasedFallbackEmptyAmsSlot:
    """Position-based mapping fallback (#1607): when no explicit mapping is
    available, the slicer's Nth filament must map to the Nth *loaded* AMS tray
    (skipping empty slots), not the Nth physical slot position. BambuStudio /
    OrcaSlicer compact their filament-assignment UI by hiding unloaded AMS
    slots, so the 3MF slot list is dense even when the AMS itself has gaps."""

    @pytest.mark.asyncio
    async def test_external_routed_correctly_when_ams_has_empty_middle_slot(self, existing_3mf_path):
        """Reporter's scenario: AMS trays 0-2 loaded, tray 3 empty, external
        loaded. Slicer emits 4 filaments — slot 4 = external. Without the fix
        the position-based fallback maps slot 4 to the empty AMS tray 3
        (since `available_trays = [0, 1, 2, 3, 254]`) and external usage is
        silently dropped because no spool is assigned to AMS0-T3.
        After the fix, empty AMS slots are filtered (tray_type is empty) so
        `available_trays = [0, 1, 2, 254]` and slot 4 correctly resolves to
        the external (global tray 254 → AMS255-T0)."""
        # Spool fed via external (vt_tray 254 → AMS255-T0)
        spool = _make_spool(spool_id=42, label_weight=1000)
        assignment = _make_assignment(spool_id=42, ams_id=255, tray_id=0)
        archive = _make_archive(archive_id=70)

        # db: archive, queue_item(None), assignment, spool
        db = _mock_db_sequential([archive, None, assignment, spool])

        # AMS reports 4 physical tray slots but slot 3 has no spool (empty
        # tray_type); external spool is loaded in vt_tray.
        # No `mapping` field on the state — forces fallback through path 5.
        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            raw_data={
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0, "tray_type": "PLA"},
                            {"id": 1, "tray_type": "PETG"},
                            {"id": 2, "tray_type": "ABS"},
                            {"id": 3, "tray_type": ""},  # empty slot
                        ],
                    }
                ],
                "vt_tray": [{"id": 254, "tray_type": "PLA"}],
            },
            progress=100,
            layer_num=50,
            tray_now=254,
            tray_change_log=[],
        )

        # 3MF has 4 dense filament slots — slot 4 is the external. Only slot 4
        # has weight (other slots came from AMS spools handled separately).
        filament_usage = [{"slot_id": 4, "used_g": 12.3, "type": "PLA", "color": "#00AABB"}]
        handled_trays: set[tuple[int, int]] = set()

        with (
            patch(
                "backend.app.utils.threemf_tools.extract_filament_usage_from_3mf",
                return_value=filament_usage,
            ),
        ):
            results = await _track_from_3mf(
                printer_id=1,
                archive_id=70,
                status="completed",
                print_name="External + AMS print",
                handled_trays=handled_trays,
                printer_manager=printer_manager,
                db=db,
            )

        assert len(results) == 1
        # The external spool was charged, NOT the empty AMS slot.
        assert results[0]["spool_id"] == 42
        assert results[0]["ams_id"] == 255
        assert results[0]["tray_id"] == 0
        assert results[0]["weight_used"] == 12.3
        assert (255, 0) in handled_trays
        # Critical assertion: AMS0-T3 (the empty slot) was NOT charged.
        assert (0, 3) not in handled_trays

    @pytest.mark.asyncio
    async def test_dense_ams_unchanged_no_empty_slots(self, existing_3mf_path):
        """Sanity check: when every AMS slot is loaded, the position-based
        fallback still works for the slicer's external = last slot case."""
        spool = _make_spool(spool_id=99, label_weight=1000)
        assignment = _make_assignment(spool_id=99, ams_id=255, tray_id=0)
        archive = _make_archive(archive_id=71)

        db = _mock_db_sequential([archive, None, assignment, spool])

        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            raw_data={
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0, "tray_type": "PLA"},
                            {"id": 1, "tray_type": "PETG"},
                            {"id": 2, "tray_type": "ABS"},
                            {"id": 3, "tray_type": "TPU"},
                        ],
                    }
                ],
                "vt_tray": [{"id": 254, "tray_type": "PLA"}],
            },
            progress=100,
            layer_num=50,
            tray_now=254,
            tray_change_log=[],
        )

        # 5 filaments, slot 5 = external. available_trays = [0,1,2,3,254] →
        # slot_id=5 → available_trays[4] = 254.
        filament_usage = [{"slot_id": 5, "used_g": 7.5, "type": "PLA", "color": ""}]
        handled_trays: set[tuple[int, int]] = set()

        with (
            patch(
                "backend.app.utils.threemf_tools.extract_filament_usage_from_3mf",
                return_value=filament_usage,
            ),
        ):
            results = await _track_from_3mf(
                printer_id=1,
                archive_id=71,
                status="completed",
                print_name="Dense AMS + external",
                handled_trays=handled_trays,
                printer_manager=printer_manager,
                db=db,
            )

        assert len(results) == 1
        assert results[0]["spool_id"] == 99
        assert results[0]["ams_id"] == 255
        assert results[0]["tray_id"] == 0


class TestNotificationVariables:
    """Tests for filament_details formatting in notifications."""

    def test_filament_details_single_slot(self):
        """Single slot produces 'PLA: 15.2g' format."""
        slots = [{"type": "PLA", "used_g": 15.2, "slot_id": 1, "color": "#FF0000"}]
        parts = []
        for slot in slots:
            ftype = slot.get("type", "Unknown") or "Unknown"
            used = slot.get("used_g", 0)
            parts.append(f"{ftype}: {used:.1f}g")
        result = " | ".join(parts)
        assert result == "PLA: 15.2g"

    def test_filament_details_multi_slot(self):
        """Multiple slots produce 'PLA: 10.0g | PETG: 5.0g' format."""
        slots = [
            {"type": "PLA", "used_g": 10.0, "slot_id": 1, "color": ""},
            {"type": "PETG", "used_g": 5.0, "slot_id": 2, "color": ""},
        ]
        parts = []
        for slot in slots:
            ftype = slot.get("type", "Unknown") or "Unknown"
            used = slot.get("used_g", 0)
            parts.append(f"{ftype}: {used:.1f}g")
        result = " | ".join(parts)
        assert result == "PLA: 10.0g | PETG: 5.0g"

    def test_filament_details_empty_type(self):
        """Empty type defaults to 'Unknown'."""
        slots = [{"type": "", "used_g": 5.0, "slot_id": 1, "color": ""}]
        parts = []
        for slot in slots:
            ftype = slot.get("type", "Unknown") or "Unknown"
            used = slot.get("used_g", 0)
            parts.append(f"{ftype}: {used:.1f}g")
        result = " | ".join(parts)
        assert result == "Unknown: 5.0g"

    def test_filament_grams_scaled_for_partial(self):
        """filament_grams is scaled by progress for partial prints."""
        filament_used_grams = 20.0
        progress = 50
        scale = max(0.0, min(progress / 100.0, 1.0))
        scaled = round(filament_used_grams * scale, 1)
        assert scaled == 10.0

    def test_filament_grams_zero_progress(self):
        """Progress=0 at cancellation gives 0.0g."""
        filament_used_grams = 20.0
        progress = 0
        scale = max(0.0, min(progress / 100.0, 1.0))
        scaled = round(filament_used_grams * scale, 1)
        assert scaled == 0.0

    def test_slot_scaling_for_partial(self):
        """Per-slot usage is scaled linearly for partial prints."""
        slots = [
            {"type": "PLA", "used_g": 20.0, "slot_id": 1, "color": ""},
            {"type": "PETG", "used_g": 10.0, "slot_id": 2, "color": ""},
        ]
        progress = 30
        scale = max(0.0, min(progress / 100.0, 1.0))
        scaled_slots = [{**s, "used_g": round(s["used_g"] * scale, 1)} for s in slots]
        assert scaled_slots[0]["used_g"] == 6.0
        assert scaled_slots[1]["used_g"] == 3.0


class TestOnPrintStartAmsMapping:
    """Tests for ams_mapping capture in on_print_start()."""

    @pytest.fixture(autouse=True)
    def _clear_sessions(self):
        _active_sessions.clear()
        yield
        _active_sessions.clear()

    @pytest.mark.asyncio
    async def test_captures_ams_mapping_from_data(self):
        """on_print_start captures ams_mapping from the data dict into the session."""
        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            raw_data={"ams": [{"id": 0, "tray": [{"id": 0, "remain": 80}]}]},
            tray_now=0,
        )

        await on_print_start(1, {"subtask_name": "Test", "ams_mapping": [3, -1, -1, 2]}, printer_manager)

        assert _active_sessions[1].ams_mapping == [3, -1, -1, 2]

    @pytest.mark.asyncio
    async def test_ams_mapping_none_when_not_in_data(self):
        """Session ams_mapping is None when data dict has no ams_mapping."""
        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            raw_data={"ams": [{"id": 0, "tray": [{"id": 0, "remain": 80}]}]},
            tray_now=0,
        )

        await on_print_start(1, {"subtask_name": "Test"}, printer_manager)

        assert _active_sessions[1].ams_mapping is None

    @pytest.mark.asyncio
    async def test_captures_queue_plate_id(self):
        """on_print_start records the queue item's plate_id onto the session (#1697)."""
        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            raw_data={"ams": [{"id": 0, "tray": [{"id": 0, "remain": 80}]}]},
            tray_now=0,
        )

        queue_item = _make_queue_item(plate_id=2)
        # on_print_start executes a SpoolAssignment lookup, then plate_id is
        # resolved via farm_correlation.resolve_active_plate_id, which selects the
        # printing PrintQueueItem candidates (.all()) — the sole one wins here.
        db = AsyncMock()
        assignment_result = MagicMock()
        assignment_result.scalars.return_value.all.return_value = []
        queue_result = MagicMock()
        queue_result.scalars.return_value.all.return_value = [queue_item]
        db.execute = AsyncMock(side_effect=[assignment_result, queue_result])

        await on_print_start(1, {"subtask_name": "Test"}, printer_manager, db=db)

        assert _active_sessions[1].plate_id == 2

    @pytest.mark.asyncio
    async def test_plate_id_none_when_no_queue_item(self):
        """Direct/library prints with no queue item leave session.plate_id = None."""
        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            raw_data={"ams": [{"id": 0, "tray": [{"id": 0, "remain": 80}]}]},
            tray_now=0,
        )

        db = AsyncMock()
        assignment_result = MagicMock()
        assignment_result.scalars.return_value.all.return_value = []
        queue_result = MagicMock()
        queue_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(side_effect=[assignment_result, queue_result])

        await on_print_start(1, {"subtask_name": "Test"}, printer_manager, db=db)

        assert _active_sessions[1].plate_id is None


class TestFindThreemfByFilename:
    """Tests for _find_3mf_by_filename() — library/archive search without archive_id."""

    @pytest.mark.asyncio
    async def test_finds_library_file(self):
        """Finds a 3MF from library files matching filename."""
        from pathlib import Path
        from unittest.mock import MagicMock

        lib_file = MagicMock()
        lib_file.file_path = "library/BMCU-BADGE.3mf"

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [lib_file]

        db = AsyncMock()
        db.execute = AsyncMock(return_value=mock_result)

        base_dir = MagicMock(spec=Path)
        candidate = MagicMock(spec=Path)
        candidate.exists.return_value = True
        candidate.suffix = ".3mf"
        base_dir.__truediv__ = MagicMock(return_value=candidate)

        result = await _find_3mf_by_filename(1, "BMCU-BADGE.3mf", db, base_dir)

        assert result == candidate

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_filename(self):
        """Returns None when filename is empty or just extensions."""
        db = AsyncMock()
        base_dir = MagicMock()

        result = await _find_3mf_by_filename(1, ".3mf", db, base_dir)
        assert result is None

        result = await _find_3mf_by_filename(1, "", db, base_dir)
        assert result is None

    @pytest.mark.asyncio
    async def test_falls_through_to_archive_search(self):
        """Falls back to previous archives when library search returns no results."""
        from pathlib import Path

        # Library returns nothing
        empty_result = MagicMock()
        empty_result.scalars.return_value.all.return_value = []

        # Archive returns a match
        archive = MagicMock()
        archive.id = 35
        # The row must NAME the print: identity is now compared in Python on the
        # returned rows rather than expressed as a SQL LIKE pattern, so a row with no
        # real filename identifies nothing. Archives are matched on ``filename``, the
        # one field the previous SQL consulted.
        archive.filename = "BMCU-BADGE.3mf"
        archive.file_path = "archives/35/BMCU-BADGE.3mf"
        archive_result = MagicMock()
        archive_result.scalars.return_value.all.return_value = [archive]

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[empty_result, archive_result])

        base_dir = MagicMock(spec=Path)
        candidate = MagicMock(spec=Path)
        candidate.exists.return_value = True
        candidate.suffix = ".3mf"
        base_dir.__truediv__ = MagicMock(return_value=candidate)

        result = await _find_3mf_by_filename(1, "BMCU-BADGE.3mf", db, base_dir)

        assert result == candidate
        assert db.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_returns_none_when_nothing_found(self):
        """Returns None when neither library nor archives have a matching 3MF."""
        empty_result = MagicMock()
        empty_result.scalars.return_value.all.return_value = []

        db = AsyncMock()
        db.execute = AsyncMock(return_value=empty_result)

        base_dir = MagicMock()

        result = await _find_3mf_by_filename(1, "nonexistent.3mf", db, base_dir)

        assert result is None

    @pytest.mark.asyncio
    async def test_strips_path_and_extensions(self):
        """Correctly strips path components and extensions for search."""
        empty_result = MagicMock()
        empty_result.scalars.return_value.all.return_value = []

        db = AsyncMock()
        db.execute = AsyncMock(return_value=empty_result)

        base_dir = MagicMock()

        # Should search for "BMCU-BADGE" base name even with path and .gcode.3mf
        await _find_3mf_by_filename(1, "/sdcard/BMCU-BADGE.gcode.3mf", db, base_dir)

        # Verify the execute was called (search was attempted with stripped name)
        assert db.execute.call_count == 2  # library + archive search


class TestTrackFrom3mfWithPreresolvedPath:
    """Tests for _track_from_3mf() with threemf_path (no archive needed)."""

    @pytest.mark.asyncio
    async def test_uses_preresolved_path_without_archive(self, existing_3mf_path):
        """When threemf_path is provided with archive_id=None, uses the path directly."""
        spool = _make_spool(spool_id=1, label_weight=1000)
        assignment = _make_assignment(spool_id=1, ams_id=0, tray_id=3)

        # DB: 1st call = assignment lookup (live), 2nd = spool lookup
        db = _mock_db_sequential([assignment, spool])

        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            raw_data={"ams": [{"id": 0, "tray": []}]},
            tray_now=255,
            last_loaded_tray=3,
            tray_change_log=[],
        )

        filament_usage = [{"slot_id": 1, "used_g": 5.0, "type": "PETG", "color": "#FFFFFF"}]

        with (
            patch(
                "backend.app.utils.threemf_tools.extract_filament_usage_from_3mf",
                return_value=filament_usage,
            ),
        ):
            results = await _track_from_3mf(
                printer_id=1,
                archive_id=None,
                status="completed",
                print_name="BMCU-BADGE",
                handled_trays=set(),
                printer_manager=printer_manager,
                db=db,
                ams_mapping=[3, -1, -1, -1],
                threemf_path=existing_3mf_path,
            )

        assert len(results) == 1
        assert results[0]["spool_id"] == 1
        assert results[0]["weight_used"] == 5.0

    @pytest.mark.asyncio
    async def test_skips_queue_lookup_without_archive_id(self, existing_3mf_path):
        """When archive_id is None, queue item lookup is skipped."""
        spool = _make_spool(spool_id=1, label_weight=1000)
        assignment = _make_assignment(spool_id=1, ams_id=0, tray_id=0)

        db = _mock_db_sequential([assignment, spool])

        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            raw_data={"ams": [{"id": 0, "tray": []}]},
            tray_now=0,
            last_loaded_tray=0,
            tray_change_log=[],
        )

        filament_usage = [{"slot_id": 1, "used_g": 2.0, "type": "PLA", "color": "#FF0000"}]

        with (
            patch(
                "backend.app.utils.threemf_tools.extract_filament_usage_from_3mf",
                return_value=filament_usage,
            ),
        ):
            # Should NOT fail even though there's no archive_id for queue lookup
            results = await _track_from_3mf(
                printer_id=1,
                archive_id=None,
                status="completed",
                print_name="Test",
                handled_trays=set(),
                printer_manager=printer_manager,
                db=db,
                tray_now_at_start=0,
                threemf_path=existing_3mf_path,
            )

        assert len(results) == 1
        assert results[0]["weight_used"] == 2.0


class TestTrackFrom3mfPlateId:
    """plate_id must propagate from PrintSession through _track_from_3mf to the
    3MF parser, so multi-plate files dispatched for one plate only count that
    plate's filament (#1697)."""

    @pytest.mark.asyncio
    async def test_passes_plate_id_to_3mf_extract(self, existing_3mf_path):
        spool = _make_spool(spool_id=1, label_weight=1000)
        assignment = _make_assignment(spool_id=1, ams_id=0, tray_id=0)

        db = _mock_db_sequential([assignment, spool])

        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            raw_data={"ams": [{"id": 0, "tray": []}]},
            tray_now=0,
            last_loaded_tray=0,
            tray_change_log=[],
        )

        extract_mock = MagicMock(return_value=[{"slot_id": 1, "used_g": 190.0, "type": "PETG", "color": "#888888"}])

        with (
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", extract_mock),
        ):
            await _track_from_3mf(
                printer_id=1,
                archive_id=None,
                status="completed",
                print_name="GridfinityLid",
                handled_trays=set(),
                printer_manager=printer_manager,
                db=db,
                tray_now_at_start=0,
                threemf_path=existing_3mf_path,
                plate_id=2,
            )

        # plate_id=2 passed positionally as second arg
        assert extract_mock.call_count == 1
        assert extract_mock.call_args.args[1] == 2

    @pytest.mark.asyncio
    async def test_plate_id_none_for_non_queue_print(self, existing_3mf_path):
        spool = _make_spool(spool_id=1, label_weight=1000)
        assignment = _make_assignment(spool_id=1, ams_id=0, tray_id=0)

        db = _mock_db_sequential([assignment, spool])

        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            raw_data={"ams": [{"id": 0, "tray": []}]},
            tray_now=0,
            last_loaded_tray=0,
            tray_change_log=[],
        )

        extract_mock = MagicMock(return_value=[{"slot_id": 1, "used_g": 5.0, "type": "PLA", "color": "#FF0000"}])

        with (
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", extract_mock),
        ):
            # No plate_id kwarg — direct/library Print flow.
            await _track_from_3mf(
                printer_id=1,
                archive_id=None,
                status="completed",
                print_name="DirectPrint",
                handled_trays=set(),
                printer_manager=printer_manager,
                db=db,
                tray_now_at_start=0,
                threemf_path=existing_3mf_path,
            )

        assert extract_mock.call_args.args[1] is None


class TestParseAmsMapping:
    """`_parse_ams_mapping` reads the stored JSON mapping, answering None for anything else."""

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            pytest.param("[0, 6, -1]", [0, 6, -1], id="a_json_list_parses"),
            pytest.param(None, None, id="nothing_stored_is_no_mapping"),
            pytest.param("", None, id="an_empty_string_is_no_mapping"),
            pytest.param("not json", None, id="malformed_json_is_no_mapping"),
            pytest.param('{"a": 1}', None, id="a_json_object_is_not_a_mapping"),
        ],
    )
    def test_parse(self, stored, expected):
        assert _parse_ams_mapping(stored) == expected


class TestCountPlatesInSliceInfo:
    """Unit tests for threemf_tools.count_plates_in_slice_info()."""

    def test_counts_multiple_plates(self, tmp_path):
        p = _write_3mf_with_plates(
            tmp_path / "multi.3mf",
            {1: [(1, 10.0, "PLA", "#FFFFFF")], 2: [(1, 20.0, "PLA", "#000000")], 3: [(1, 5.0, "PLA", "#FF0000")]},
        )
        assert count_plates_in_slice_info(p) == 3

    def test_single_plate(self, tmp_path):
        p = _write_3mf_with_plates(tmp_path / "single.3mf", {1: [(1, 10.0, "PLA", "#FFFFFF")]})
        assert count_plates_in_slice_info(p) == 1

    def test_missing_config_returns_zero(self, tmp_path):
        import zipfile as _zip

        p = tmp_path / "no_config.3mf"
        with _zip.ZipFile(p, "w") as zf:
            zf.writestr("other.txt", "x")
        assert count_plates_in_slice_info(p) == 0

    def test_missing_file_returns_zero(self, tmp_path):
        assert count_plates_in_slice_info(tmp_path / "does_not_exist.3mf") == 0


class TestResolveRunContext:
    """_resolve_run_context() session fast-path (no query) and precedence."""

    @pytest.mark.asyncio
    async def test_session_fast_path_no_query(self):
        """A live session returns its own values without touching the DB."""
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=AssertionError("resolver must not query when a session exists"))
        session = PrintSession(
            printer_id=1,
            print_name="X",
            started_at=datetime.now(timezone.utc),
            ams_mapping=[3, -1],
            plate_id=7,
        )
        plate_id, ams_mapping = await _resolve_run_context(db, 1, {"subtask_id": "ignored"}, 99, session)
        assert plate_id == 7
        assert ams_mapping == [3, -1]


async def _seed_completion(
    db,
    printer_id: int,
    tmp_path,
    *,
    plates: dict,
    rel_path: str = "archives/x/file.gcode.3mf",
    queue_plate_id=None,
    subtask: str = "ST-SUB",
    ams_mapping: str = "[0]",
    assign_ams_id: int = 0,
    assign_tray_id: int = 0,
    archive_started_at=None,
    spool_weight_used: float = 0.0,
    with_queue_item: bool = True,
):
    """Seed a printer's completed print: real multi/single-plate 3MF on disk, a
    PrintArchive (status='printing'), a Spool + SpoolAssignment, and optionally
    the dispatched PrintQueueItem carrying the durable plate_id/ams_mapping.
    """
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.spool import Spool
    from backend.app.models.spool_assignment import SpoolAssignment

    _write_3mf_with_plates(tmp_path / rel_path, plates)

    archive = PrintArchive(
        printer_id=printer_id,
        filename=rel_path.split("/")[-1],
        file_path=rel_path,
        file_size=1000,
        status="printing",
        print_name="SeededPrint",
        started_at=archive_started_at or (datetime.now(timezone.utc) - timedelta(minutes=5)),
        filament_used_grams=50.0,
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)

    spool = Spool(material="PLA", label_weight=1000, weight_used=spool_weight_used)
    db.add(spool)
    await db.commit()
    await db.refresh(spool)

    db.add(SpoolAssignment(spool_id=spool.id, printer_id=printer_id, ams_id=assign_ams_id, tray_id=assign_tray_id))
    item = None
    if with_queue_item:
        item = PrintQueueItem(
            printer_id=printer_id,
            archive_id=archive.id,
            status="completed",
            plate_id=queue_plate_id,
            ams_mapping=ams_mapping,
            dispatch_subtask_id=subtask,
            started_at=datetime.now(timezone.utc),
        )
        db.add(item)
    await db.commit()
    return archive, spool, item


def _completion_pm():
    """Mock printer_manager for a settled completed print (no live mapping)."""
    pm = MagicMock()
    pm.get_status.return_value = SimpleNamespace(
        raw_data={},
        progress=100,
        layer_num=1,
        tray_now=0,
        last_loaded_tray=0,
        tray_change_log=[],
        total_layers=1,
    )
    return pm


async def _count_history(db) -> int:
    from sqlalchemy import func, select

    from backend.app.models.spool_usage_history import SpoolUsageHistory

    result = await db.execute(select(func.count()).select_from(SpoolUsageHistory))
    return result.scalar() or 0


class TestUsageIntegrityIntegration:
    """Real-DB integration for the durable resolver + idempotency guard."""

    @pytest.fixture(autouse=True)
    def _clear_sessions(self):
        _active_sessions.clear()
        yield
        _active_sessions.clear()

    @pytest.mark.asyncio
    async def test_no_session_matched_by_subtask_is_plate_scoped(
        self, db_session, printer_factory, tmp_path, monkeypatch
    ):
        """No session, but the terminal subtask_id matches a queue item → only
        the printed plate's grams are charged, NOT the whole multi-plate file."""
        from backend.app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await printer_factory()
        # Plate 1 = 50 g, plate 2 = 999 g → whole-file sum 1049 g.
        archive, spool, _ = await _seed_completion(
            db_session,
            printer.id,
            tmp_path,
            plates={1: [(1, 50.0, "PLA", "#FF0000")], 2: [(1, 999.0, "PLA", "#00FF00")]},
            queue_plate_id=1,
            subtask="ST-SUB-1",
        )

        results = await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed", "subtask_id": "ST-SUB-1"},
            printer_manager=_completion_pm(),
            db=db_session,
            archive_id=archive.id,
        )

        await db_session.refresh(spool)
        assert sum(r["weight_used"] for r in results) == pytest.approx(50.0, abs=0.1)
        assert spool.weight_used == pytest.approx(50.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_reconcile_shaped_payload_derives_plate_and_mapping(
        self, db_session, printer_factory, tmp_path, monkeypatch
    ):
        """Reconcile-shaped payload (subtask_id present, no session, no
        ams_mapping arg) derives BOTH the plate and the AMS mapping from the
        durable queue item: plate 2's 30 g lands on the mapped tray AMS1-T2."""
        from backend.app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await printer_factory()
        # Mapping "[6]" → slicer slot 1 -> global tray 6 -> AMS1-T2.
        archive, spool, _ = await _seed_completion(
            db_session,
            printer.id,
            tmp_path,
            plates={1: [(1, 111.0, "PLA", "#FF0000")], 2: [(1, 30.0, "PLA", "#00FF00")]},
            queue_plate_id=2,
            subtask="ST-REC-9",
            ams_mapping="[6]",
            assign_ams_id=1,
            assign_tray_id=2,
        )

        results = await on_print_complete(
            printer_id=printer.id,
            data={
                "status": "completed",
                "subtask_id": "ST-REC-9",
                "last_progress": 100.0,
                "last_layer_num": 1,
                "raw_data": {},
                "_reconciled": True,
            },
            printer_manager=_completion_pm(),
            db=db_session,
            archive_id=archive.id,
        )

        await db_session.refresh(spool)
        assert len(results) == 1
        assert results[0]["ams_id"] == 1
        assert results[0]["tray_id"] == 2
        assert results[0]["weight_used"] == pytest.approx(30.0, abs=0.1)
        assert spool.weight_used == pytest.approx(30.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_second_completion_is_noop(self, db_session, printer_factory, tmp_path, monkeypatch):
        """A duplicate completion for the same run inserts no new history rows
        and does not change weight_used (idempotency guard)."""
        from backend.app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await printer_factory()
        archive, spool, _ = await _seed_completion(
            db_session,
            printer.id,
            tmp_path,
            plates={1: [(1, 50.0, "PLA", "#FF0000")]},
            queue_plate_id=1,
            subtask="ST-DUP",
        )

        first = await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed", "subtask_id": "ST-DUP"},
            printer_manager=_completion_pm(),
            db=db_session,
            archive_id=archive.id,
        )
        assert len(first) == 1
        await db_session.refresh(spool)
        weight_after_first = spool.weight_used
        rows_after_first = await _count_history(db_session)

        second = await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed", "subtask_id": "ST-DUP"},
            printer_manager=_completion_pm(),
            db=db_session,
            archive_id=archive.id,
        )

        await db_session.refresh(spool)
        assert second == []
        assert await _count_history(db_session) == rows_after_first
        assert spool.weight_used == weight_after_first

    @pytest.mark.asyncio
    async def test_reprint_finalizes_again(self, db_session, printer_factory, tmp_path, monkeypatch):
        """A reprint resets archive.started_at to now, so a prior run's older
        usage row no longer blocks finalizing the fresh run."""
        from backend.app.core.config import settings as app_settings
        from backend.app.models.spool_usage_history import SpoolUsageHistory

        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await printer_factory()
        archive, spool, _ = await _seed_completion(
            db_session,
            printer.id,
            tmp_path,
            plates={1: [(1, 50.0, "PLA", "#FF0000")]},
            queue_plate_id=1,
            subtask="ST-RE",
            spool_weight_used=50.0,
        )
        # A prior run's usage row, finalized 2h ago.
        db_session.add(
            SpoolUsageHistory(
                spool_id=spool.id,
                printer_id=printer.id,
                print_name="prev",
                weight_used=50.0,
                percent_used=5,
                status="completed",
                archive_id=archive.id,
                created_at=datetime.utcnow() - timedelta(hours=2),
            )
        )
        # Reprint resets started_at to now (main.py:3011-3012 behavior).
        archive.started_at = datetime.now(timezone.utc)
        await db_session.commit()

        results = await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed", "subtask_id": "ST-RE"},
            printer_manager=_completion_pm(),
            db=db_session,
            archive_id=archive.id,
        )

        await db_session.refresh(spool)
        assert len(results) == 1
        assert await _count_history(db_session) == 2
        assert spool.weight_used == pytest.approx(100.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_multi_plate_unknowable_skips_and_warns(
        self, db_session, printer_factory, tmp_path, monkeypatch, caplog
    ):
        """Multi-plate file with no session AND no queue item → 3MF path skipped
        (no over-charge), a WARNING is logged, and no usage rows are written."""
        import logging

        from backend.app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await printer_factory()
        archive, spool, _ = await _seed_completion(
            db_session,
            printer.id,
            tmp_path,
            plates={1: [(1, 50.0, "PLA", "#FF0000")], 2: [(1, 999.0, "PLA", "#00FF00")]},
            with_queue_item=False,
        )

        with caplog.at_level(logging.WARNING, logger="backend.app.services.usage_tracker"):
            results = await on_print_complete(
                printer_id=printer.id,
                data={"status": "completed"},  # no subtask_id → unresolvable
                printer_manager=_completion_pm(),
                db=db_session,
                archive_id=archive.id,
            )

        await db_session.refresh(spool)
        assert results == []
        assert await _count_history(db_session) == 0
        assert spool.weight_used == 0.0
        assert any("plates but the printed plate is unknown" in r.message for r in caplog.records)

    @staticmethod
    async def _seed_retry_of(db, printer_id: int, *, record_archive, retry_job: str, retry_plate: int):
        """A failed parent and its retry, both carrying the parent's printed archive as the DONOR,
        with ``record_archive`` stamped as the retry's own attempt (one archive per attempt)."""
        from backend.app.models.archive import PrintArchive
        from backend.app.models.print_queue import PrintQueueItem

        donor = PrintArchive(
            printer_id=printer_id,
            filename="donor.gcode.3mf",
            file_path="archives/donor/donor.gcode.3mf",
            file_size=1,
            status="failed",
            subtask_id="PARENT-1",
        )
        db.add(donor)
        await db.flush()
        parent = PrintQueueItem(
            printer_id=printer_id,
            archive_id=donor.id,
            status="failed",
            plate_id=2,  # the WRONG plate for this print, so a donor-link match is visible
            ams_mapping="[4]",
            dispatch_subtask_id="PARENT-1",
            started_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        retry = PrintQueueItem(
            printer_id=printer_id,
            archive_id=donor.id,
            status="completed",
            plate_id=retry_plate,
            ams_mapping="[0]",
            dispatch_subtask_id=retry_job,
            started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        db.add_all([parent, retry])
        record_archive.subtask_id = retry_job
        await db.commit()
        return parent, retry

    @pytest.mark.asyncio
    async def test_an_id_less_terminal_of_a_retry_resolves_the_retry_by_its_record(
        self, db_session, printer_factory, tmp_path, monkeypatch
    ):
        """A retry prints into its OWN archive row; the donor link (``archive_id``) is its parent's.
        A terminal whose echo carries no id (firmware reset on cancel) is tied back to the retry
        through the record's job id — the retry's plate is charged, not the whole file and not the
        parent's plate."""
        from backend.app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await printer_factory()
        # Plate 1 = 50 g (the retry's), plate 2 = 999 g (the parent's).
        archive, spool, _ = await _seed_completion(
            db_session,
            printer.id,
            tmp_path,
            plates={1: [(1, 50.0, "PLA", "#FF0000")], 2: [(1, 999.0, "PLA", "#00FF00")]},
            with_queue_item=False,
        )
        await self._seed_retry_of(db_session, printer.id, record_archive=archive, retry_job="RETRY-1", retry_plate=1)

        results = await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed"},  # the echo named no job
            printer_manager=_completion_pm(),
            db=db_session,
            archive_id=archive.id,
        )

        await db_session.refresh(spool)
        assert sum(r["weight_used"] for r in results) == pytest.approx(50.0, abs=0.1)
        assert spool.weight_used == pytest.approx(50.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_the_queue_mapping_tier_reads_the_attempts_unit_over_a_shared_donor(
        self, db_session, printer_factory, tmp_path, monkeypatch
    ):
        """The 3MF lane's queue-mapping tier (no print-command mapping, no MQTT mapping) finds the
        unit THIS archive records. Keyed on the donor link it raised MultipleResultsFound as soon as a
        unit and its retry shared the donor — here the record is the first attempt's adopted copy, so
        parent and retry both point at it."""
        from backend.app.core.config import settings as app_settings
        from backend.app.models.print_queue import PrintQueueItem

        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await printer_factory()
        archive, spool, _ = await _seed_completion(
            db_session,
            printer.id,
            tmp_path,
            plates={1: [(1, 30.0, "PLA", "#FF0000")]},
            assign_ams_id=1,
            assign_tray_id=2,
            with_queue_item=False,
        )
        archive.subtask_id = "FIRST-1"
        first = PrintQueueItem(
            printer_id=printer.id,
            archive_id=archive.id,  # adopted: donor AND record
            status="failed",
            ams_mapping="[6]",  # slicer slot 1 -> global tray 6 -> AMS1-T2
            dispatch_subtask_id="FIRST-1",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        )
        retry = PrintQueueItem(
            printer_id=printer.id,
            archive_id=archive.id,  # the donor it inherited
            status="printing",
            ams_mapping="[0]",
            dispatch_subtask_id="RETRY-2",
            started_at=datetime.now(timezone.utc),
        )
        db_session.add_all([first, retry])
        await db_session.commit()
        # A live session that captured no mapping: the charge must reach the queue-mapping tier.
        _active_sessions[printer.id] = PrintSession(
            printer_id=printer.id, print_name="SeededPrint", started_at=datetime.now(timezone.utc), plate_id=1
        )

        results = await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed"},
            printer_manager=_completion_pm(),
            db=db_session,
            archive_id=archive.id,
        )

        await db_session.refresh(spool)
        assert [(r["ams_id"], r["tray_id"]) for r in results] == [(1, 2)], "the FIRST attempt's mapping"
        assert spool.weight_used == pytest.approx(30.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_single_plate_no_session_tracks_full(self, db_session, printer_factory, tmp_path, monkeypatch):
        """A single-plate file with no session still tracks the full usage — the
        multi-plate skip must NOT fire for a one-plate file."""
        from backend.app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await printer_factory()
        archive, spool, _ = await _seed_completion(
            db_session,
            printer.id,
            tmp_path,
            plates={1: [(1, 42.0, "PLA", "#FF0000")]},
            queue_plate_id=None,  # unknown plate index, but only one plate exists
            subtask="ST-ONE",
        )

        results = await on_print_complete(
            printer_id=printer.id,
            data={"status": "completed", "subtask_id": "ST-ONE"},
            printer_manager=_completion_pm(),
            db=db_session,
            archive_id=archive.id,
        )

        await db_session.refresh(spool)
        assert len(results) == 1
        assert spool.weight_used == pytest.approx(42.0, abs=0.1)


class TestAmsWeightSyncAllowed:
    """Truth table for usage_tracker.ams_weight_sync_allowed()."""

    @pytest.mark.asyncio
    async def test_active_state_blocks(self, db_session, printer_factory):
        from backend.app.services.usage_tracker import ams_weight_sync_allowed

        printer = await printer_factory()
        for active in ("RUNNING", "PAUSE", "PREPARE", "SLICING"):
            assert await ams_weight_sync_allowed(db_session, printer.id, SimpleNamespace(state=active)) is False

    @pytest.mark.asyncio
    async def test_none_and_unknown_state_block(self, db_session, printer_factory):
        from backend.app.services.usage_tracker import ams_weight_sync_allowed

        printer = await printer_factory()
        assert await ams_weight_sync_allowed(db_session, printer.id, None) is False
        assert await ams_weight_sync_allowed(db_session, printer.id, SimpleNamespace(state=None)) is False
        assert await ams_weight_sync_allowed(db_session, printer.id, SimpleNamespace(state="unknown")) is False

    @pytest.mark.asyncio
    async def test_idle_with_printing_archive_blocks(self, db_session, printer_factory):
        from backend.app.models.archive import PrintArchive
        from backend.app.services.usage_tracker import ams_weight_sync_allowed

        printer = await printer_factory()
        db_session.add(
            PrintArchive(
                printer_id=printer.id,
                filename="p.gcode.3mf",
                file_path="a/p.gcode.3mf",
                file_size=1,
                status="printing",
            )
        )
        await db_session.commit()
        assert await ams_weight_sync_allowed(db_session, printer.id, SimpleNamespace(state="IDLE")) is False

    @pytest.mark.asyncio
    async def test_idle_no_printing_archive_allows(self, db_session, printer_factory):
        from backend.app.models.archive import PrintArchive
        from backend.app.services.usage_tracker import ams_weight_sync_allowed

        printer = await printer_factory()
        # A settled archive for the same printer must not block the sync.
        db_session.add(
            PrintArchive(
                printer_id=printer.id,
                filename="p.gcode.3mf",
                file_path="a/p.gcode.3mf",
                file_size=1,
                status="completed",
            )
        )
        await db_session.commit()
        assert await ams_weight_sync_allowed(db_session, printer.id, SimpleNamespace(state="IDLE")) is True


_NEAR_CHIP = "EC96F1E700000100"
_FAR_CHIP = "3CF1F3E700000100"
_ROLL_UUID = "8AC9EC0847FD41D0890870319F2E1975"


def _identity_tray(*, tag_uid=None, tray_uuid=None):
    return {"id": 0, "state": 11, "tray_type": "PETG", "tag_uid": tag_uid, "tray_uuid": tray_uuid}


class TestWireIdentityIsTheBoundRow:
    """``wire_identity_is_the_bound_row`` gates every tagged ledger write, so a false
    "not the same roll" answer silently switches the weight-sync and decrease-reconcile
    lanes OFF for a roll that IS the bound one.

    A Bambu roll carries TWO RFID chips sharing one ``tray_uuid``, and both are
    its identity (doctrine rule 10), so the gate has to accept either chip. The
    uuid decides first where the push carries one; a bare chip read is the
    incremental shape the wire sends most often.
    """

    def _spool(self, **kw):
        from backend.app.models.spool import Spool

        return Spool(material="PETG", label_weight=1000, core_weight=250, **kw)

    def test_the_near_chip_holds_the_gate(self):
        spool = self._spool(tag_uid=_NEAR_CHIP, sibling_tag_uid=_FAR_CHIP)
        assert wire_identity_is_the_bound_row(spool, _identity_tray(tag_uid=_NEAR_CHIP)) is True

    def test_the_sibling_chip_holds_the_gate_too(self):
        """The push carries the roll's FAR side and no uuid, which is still the bound roll."""
        spool = self._spool(tag_uid=_NEAR_CHIP, sibling_tag_uid=_FAR_CHIP)
        assert wire_identity_is_the_bound_row(spool, _identity_tray(tag_uid=_FAR_CHIP)) is True

    def test_an_unrecorded_chip_does_not_hold_the_gate(self):
        """Before the pair is learned the far chip is genuinely unknown; answering True
        on faith would let a swapped-in roll rewrite the departing row's ledger."""
        spool = self._spool(tag_uid=_NEAR_CHIP)
        assert wire_identity_is_the_bound_row(spool, _identity_tray(tag_uid=_FAR_CHIP)) is False

    def test_a_third_party_chip_never_holds_the_gate(self):
        spool = self._spool(tag_uid=_NEAR_CHIP, sibling_tag_uid=_FAR_CHIP)
        assert wire_identity_is_the_bound_row(spool, _identity_tray(tag_uid="A5E7210D00000100")) is False

    def test_uuid_still_decides_first(self):
        """Unchanged precedence: an agreeing uuid settles it whatever the chips say."""
        spool = self._spool(tag_uid=_NEAR_CHIP, tray_uuid=_ROLL_UUID)
        tray = _identity_tray(tag_uid="A5E7210D00000100", tray_uuid=_ROLL_UUID)
        assert wire_identity_is_the_bound_row(spool, tray) is True

    def test_nothing_comparable_still_refuses(self):
        """Silence is never agreement — a tagless row and an untagged tray stay False."""
        assert wire_identity_is_the_bound_row(self._spool(), _identity_tray()) is False
        assert wire_identity_is_the_bound_row(self._spool(sibling_tag_uid=_FAR_CHIP), _identity_tray()) is False
