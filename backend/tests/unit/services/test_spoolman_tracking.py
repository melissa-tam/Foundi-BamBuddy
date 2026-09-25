"""Unit tests for Spoolman tracking service helpers."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.spoolman_tracking import (
    _apply_spool_colors_to_archive,
    _get_fallback_spool_tag,
    _global_tray_id_to_ams_slot,
    _hash_serial_to_hex32,
    _resolve_global_tray_id,
    _resolve_spool_tag,
    build_ams_tray_lookup,
    store_print_data,
)


def _ams_printer_manager(slot_count=1):
    """Printer manager reporting one AMS unit with PLA seated in `slot_count` slots."""
    printer_manager = MagicMock()
    printer_manager.get_status.return_value = SimpleNamespace(
        raw_data={"ams": [{"id": 0, "tray": [{"id": i, "tray_type": "PLA"} for i in range(slot_count)]}]}
    )
    return printer_manager


@pytest.fixture
def app_settings_with_existing_file():
    """`app_settings` whose base_dir / <relative path> resolves to a file that exists."""
    mock_settings = MagicMock()
    mock_path = MagicMock()
    mock_path.exists.return_value = True
    mock_settings.base_dir.__truediv__.return_value = mock_path
    return mock_settings


class TestResolveSpoolTag:
    """Tests for _resolve_spool_tag()."""

    def test_prefers_tray_uuid_over_tag_uid(self):
        tray = {"tray_uuid": "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4", "tag_uid": "DEADBEEF"}
        assert _resolve_spool_tag(tray) == "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4"

    def test_falls_back_to_tag_uid_when_no_uuid(self):
        tray = {"tray_uuid": "", "tag_uid": "DEADBEEF"}
        assert _resolve_spool_tag(tray) == "DEADBEEF"

    def test_falls_back_to_tag_uid_when_uuid_zero(self):
        tray = {"tray_uuid": "00000000000000000000000000000000", "tag_uid": "DEADBEEF"}
        assert _resolve_spool_tag(tray) == "DEADBEEF"

    def test_rejects_zero_tag_uid(self):
        tray = {"tray_uuid": "", "tag_uid": "0000000000000000"}
        assert _resolve_spool_tag(tray) == ""

    def test_uses_fallback_tag_when_ids_missing(self):
        tray = {"tray_uuid": "", "tag_uid": ""}
        # global_tray_id 0 -> ams_id 0, tray_id 0
        assert _resolve_spool_tag(tray, "01P00A000000000", 0) == "ABA7845700000000"

    def test_uses_fallback_tag_when_ids_zero(self):
        tray = {"tray_uuid": "00000000000000000000000000000000", "tag_uid": "0000000000000000"}
        # global_tray_id 5 -> ams_id 1, tray_id 1
        assert _resolve_spool_tag(tray, "01P00A000000000", 5) == "ABA7845700010001"

    def test_prefers_tray_uuid_over_fallback_when_non_zero(self):
        tray = {"tray_uuid": "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4", "tag_uid": ""}
        assert _resolve_spool_tag(tray, "01P00A000000000", 0) == "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4"

    def test_empty_both(self):
        tray = {"tray_uuid": "", "tag_uid": ""}
        assert _resolve_spool_tag(tray) == ""

    def test_missing_keys(self):
        assert _resolve_spool_tag({}) == ""

    def test_zero_uuid_no_tag(self):
        tray = {"tray_uuid": "00000000000000000000000000000000", "tag_uid": ""}
        assert _resolve_spool_tag(tray) == ""


class TestResolveGlobalTrayId:
    """Tests for _resolve_global_tray_id()."""

    def test_default_mapping(self):
        """slot 1 -> tray 0, slot 2 -> tray 1, etc."""
        assert _resolve_global_tray_id(1, None) == 0
        assert _resolve_global_tray_id(2, None) == 1
        assert _resolve_global_tray_id(4, None) == 3

    def test_custom_mapping(self):
        """Custom slot_to_tray overrides default."""
        mapping = [5, 2, -1, 0]
        assert _resolve_global_tray_id(1, mapping) == 5
        assert _resolve_global_tray_id(2, mapping) == 2
        assert _resolve_global_tray_id(4, mapping) == 0

    def test_unmapped_slot(self):
        """Slot with -1 in mapping uses default."""
        mapping = [5, -1, 2, 0]
        assert _resolve_global_tray_id(2, mapping) == 1  # default: slot 2 -> tray 1

    def test_slot_beyond_mapping(self):
        """Slot beyond mapping length uses default."""
        mapping = [5, 2]
        assert _resolve_global_tray_id(3, mapping) == 2  # default: slot 3 -> tray 2

    def test_empty_mapping(self):
        mapping = []
        assert _resolve_global_tray_id(1, mapping) == 0

    def test_minus_one_resolves_to_external_spool_when_present(self):
        """#1276 (regression of #853): -1 in slot_to_tray is BambuStudio's
        encoding for "external spool used" — look up the external spool in
        ams_trays rather than falling through to the position-based default
        (which would credit an unrelated AMS tray). Reporter ojimpo's H2S
        had AMS slot 0 occupied with PLA and ran a TPU external-spool print;
        the bug credited the TPU usage to the PLA spool.
        """
        # Single external spool (most common: H2S/X1C/P1S + external)
        assert _resolve_global_tray_id(1, [-1], ams_trays={254: {}}) == 254
        # AMS occupied with material AND external in use — fix prevents
        # crediting AMS slot 0 (the actual bug from #1276)
        assert _resolve_global_tray_id(1, [-1], ams_trays={0: {}, 1: {}, 2: {}, 3: {}, 254: {}}) == 254
        # H2D-style deputy nozzle at 255
        assert _resolve_global_tray_id(1, [-1], ams_trays={0: {}, 255: {}}) == 255
        # Both external slots present (multi-nozzle) — prefer 254 (main on
        # single-nozzle, deputy on H2D — matches tray_now reporting)
        assert _resolve_global_tray_id(1, [-1], ams_trays={254: {}, 255: {}}) == 254

    def test_minus_one_falls_through_when_no_external_in_ams_trays(self):
        """If -1 is seen but ams_trays has no external spool (254/255),
        fall through to position-based default (legacy behavior preserved
        for callers that don't pass ams_trays or pre-fix call sites).
        """
        # ams_trays without external — fall through to legacy behavior
        assert _resolve_global_tray_id(1, [-1], ams_trays={0: {}, 1: {}}) == 0
        # No ams_trays passed at all — legacy fallback
        assert _resolve_global_tray_id(1, [-1]) == 0


class TestFallbackTagHelpers:
    """Tests for frontend-mirrored fallback tag helpers."""

    def test_hash_serial_matches_frontend_algorithm(self):
        assert _hash_serial_to_hex32("01P00A000000000") == "ABA78457"
        # Frontend trims and uppercases before hashing
        assert _hash_serial_to_hex32(" 01p00a000000000 ") == "ABA78457"

    def test_global_tray_to_ams_slot_standard_ams(self):
        assert _global_tray_id_to_ams_slot(0) == (0, 0)
        assert _global_tray_id_to_ams_slot(7) == (1, 3)

    def test_global_tray_to_ams_slot_ams_ht(self):
        assert _global_tray_id_to_ams_slot(128) == (128, 0)
        assert _global_tray_id_to_ams_slot(135) == (135, 0)

    def test_global_tray_to_ams_slot_external(self):
        assert _global_tray_id_to_ams_slot(254) == (255, 0)
        assert _global_tray_id_to_ams_slot(255) == (255, 1)

    def test_get_fallback_spool_tag_standard(self):
        assert _get_fallback_spool_tag("01P00A000000000", 5) == "ABA7845700010001"

    def test_get_fallback_spool_tag_ams_ht(self):
        assert _get_fallback_spool_tag("01P00A000000000", 128) == "ABA7845700800000"

    def test_get_fallback_spool_tag_external(self):
        assert _get_fallback_spool_tag("01P00A000000000", 255) == "ABA7845700FF0001"


class TestBuildAmsTrayLookup:
    """Tests for build_ams_tray_lookup()."""

    def test_single_ams_unit(self):
        raw = {
            "ams": [
                {
                    "id": 0,
                    "tray": [
                        {"id": 0, "tray_uuid": "AAA", "tag_uid": "111", "tray_type": "PLA"},
                        {"id": 1, "tray_uuid": "BBB", "tag_uid": "222", "tray_type": "ABS"},
                    ],
                }
            ]
        }
        lookup = build_ams_tray_lookup(raw)
        assert lookup[0] == {"tray_uuid": "AAA", "tag_uid": "111", "tray_type": "PLA"}
        assert lookup[1] == {"tray_uuid": "BBB", "tag_uid": "222", "tray_type": "ABS"}

    def test_multiple_ams_units(self):
        raw = {
            "ams": [
                {"id": 0, "tray": [{"id": 0, "tray_uuid": "A", "tag_uid": "", "tray_type": "PLA"}]},
                {"id": 1, "tray": [{"id": 0, "tray_uuid": "B", "tag_uid": "", "tray_type": "PETG"}]},
            ]
        }
        lookup = build_ams_tray_lookup(raw)
        assert 0 in lookup  # AMS 0, tray 0
        assert 4 in lookup  # AMS 1, tray 0 (1*4+0)
        assert lookup[4]["tray_uuid"] == "B"

    def test_external_spool(self):
        raw = {
            "ams": [],
            "vt_tray": [{"tray_uuid": "EXT", "tag_uid": "X", "tray_type": "TPU"}],
        }
        lookup = build_ams_tray_lookup(raw)
        assert 254 in lookup
        assert lookup[254]["tray_type"] == "TPU"

    def test_empty_external_spool_skipped(self):
        raw = {"ams": [], "vt_tray": [{"tray_type": ""}]}
        lookup = build_ams_tray_lookup(raw)
        assert 254 not in lookup

    def test_no_ams_data(self):
        assert build_ams_tray_lookup({}) == {}
        assert build_ams_tray_lookup({"ams": []}) == {}

    def test_missing_fields_default(self):
        raw = {"ams": [{"id": 0, "tray": [{"id": 0}]}]}
        lookup = build_ams_tray_lookup(raw)
        assert lookup[0] == {"tray_uuid": "", "tag_uid": "", "tray_type": ""}


class TestStorePrintData:
    """Tests for store_print_data()."""

    @pytest.mark.asyncio
    async def test_prefers_explicit_ams_mapping_over_queue_mapping(self, app_settings_with_existing_file):
        db = AsyncMock()
        # store_print_data now queries the queue item unconditionally (to pick up
        # plate_id for multi-plate 3MFs, #1697), then deletes any stale spoolman
        # row before inserting the new one. Two execute calls in that order.
        queue_item = SimpleNamespace(ams_mapping=json.dumps([2, -1, -1, -1]), plate_id=None)
        queue_result = MagicMock()
        queue_result.scalar_one_or_none.return_value = queue_item
        delete_result = MagicMock()
        db.execute = AsyncMock(side_effect=[queue_result, delete_result])
        db.add = MagicMock()
        db.commit = AsyncMock()

        printer_manager = _ams_printer_manager(slot_count=2)

        with (
            patch("backend.app.services.spoolman_tracking.app_settings", app_settings_with_existing_file),
            patch("backend.app.api.routes.settings.get_setting", AsyncMock(side_effect=["true", "true"])),
            patch(
                "backend.app.utils.threemf_tools.extract_filament_usage_from_3mf",
                return_value=[{"slot_id": 1, "used_g": 3.83, "type": "PLA", "color": "#FF0000"}],
            ),
            patch("backend.app.utils.threemf_tools.extract_layer_filament_usage_from_3mf", return_value=None),
            patch("backend.app.utils.threemf_tools.extract_filament_properties_from_3mf", return_value={}),
        ):
            await store_print_data(
                printer_id=1,
                archive_id=15,
                file_path="archives/test.3mf",
                db=db,
                printer_manager=printer_manager,
                ams_mapping=[1, -1, -1, -1],
            )

        db.add.assert_called_once()
        tracking = db.add.call_args.args[0]
        assert tracking.slot_to_tray == [1, -1, -1, -1]
        assert db.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_tracking_runs_whenever_spoolman_is_enabled(self, app_settings_with_existing_file):
        """Per-print tracking is the ONLY weight writer for Spoolman (#1119).

        So it runs on the `spoolman_enabled` setting alone: gating it on the
        deprecated `spoolman_disable_weight_sync` flag as well would leave
        non-Bambu spools with no weight-update path at all.
        """
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock())
        db.add = MagicMock()
        db.commit = AsyncMock()

        printer_manager = _ams_printer_manager()

        # A single side_effect entry: spoolman_enabled is the only setting read,
        # so any extra get_setting call would exhaust the mock and fail here.
        with (
            patch("backend.app.services.spoolman_tracking.app_settings", app_settings_with_existing_file),
            patch("backend.app.api.routes.settings.get_setting", AsyncMock(side_effect=["true"])),
            patch(
                "backend.app.utils.threemf_tools.extract_filament_usage_from_3mf",
                return_value=[{"slot_id": 1, "used_g": 5.0, "type": "PLA", "color": "#FF0000"}],
            ),
            patch("backend.app.utils.threemf_tools.extract_layer_filament_usage_from_3mf", return_value=None),
            patch("backend.app.utils.threemf_tools.extract_filament_properties_from_3mf", return_value={}),
        ):
            await store_print_data(
                printer_id=1,
                archive_id=20,
                file_path="archives/test.3mf",
                db=db,
                printer_manager=printer_manager,
                ams_mapping=[0],
            )

        # Tracking row was inserted — the fix is working.
        db.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_passes_queue_plate_id_to_3mf_extract(self, app_settings_with_existing_file):
        """A multi-plate 3MF queued for one plate is charged that plate's filament only (#1697)."""
        db = AsyncMock()
        queue_item = SimpleNamespace(ams_mapping=None, plate_id=2)
        queue_result = MagicMock()
        queue_result.scalar_one_or_none.return_value = queue_item
        delete_result = MagicMock()
        db.execute = AsyncMock(side_effect=[queue_result, delete_result])
        db.add = MagicMock()
        db.commit = AsyncMock()

        extract_mock = MagicMock(return_value=[{"slot_id": 1, "used_g": 190.0, "type": "PETG", "color": "#888888"}])

        with (
            patch("backend.app.services.spoolman_tracking.app_settings", app_settings_with_existing_file),
            patch("backend.app.api.routes.settings.get_setting", AsyncMock(side_effect=["true", "true"])),
            patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", extract_mock),
            patch("backend.app.utils.threemf_tools.extract_layer_filament_usage_from_3mf", return_value=None),
            patch("backend.app.utils.threemf_tools.extract_filament_properties_from_3mf", return_value={}),
        ):
            await store_print_data(
                printer_id=1,
                archive_id=15,
                file_path="archives/test.3mf",
                db=db,
                printer_manager=_ams_printer_manager(),
                ams_mapping=[1, -1, -1, -1],
            )

        # plate_id rides as the second positional arg to the extractor
        assert extract_mock.call_count == 1
        assert extract_mock.call_args.args[1] == 2


class TestApplySpoolColorsToArchive:
    """`_apply_spool_colors_to_archive` stamps the archive's filament_color
    from the matched Spoolman spools (#1494) — the Spoolman-mode mirror of
    the built-in inventory rewrite in usage_tracker."""

    def _make_db(self, archive):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=archive)))
        return db

    @pytest.mark.asyncio
    async def test_rewrites_color_from_spoolman_spool(self):
        """The #1494 case: 3MF said #161616, the Spoolman spool is 000000."""
        archive = MagicMock()
        archive.filament_color = "#161616"
        db = self._make_db(archive)

        await _apply_spool_colors_to_archive(
            db,
            archive_id=10,
            filament_usage=[{"slot_id": 1, "used_g": 15.9}],
            slot_colors={1: "000000"},
        )

        assert archive.filament_color == "#000000"
        db.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_empty_slot_colors_is_noop(self):
        """No resolved spool colours — never touches the DB."""
        db = self._make_db(MagicMock())
        await _apply_spool_colors_to_archive(
            db, archive_id=10, filament_usage=[{"slot_id": 1, "used_g": 15.9}], slot_colors={}
        )
        db.execute.assert_not_awaited()
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_partial_match_leaves_archive_untouched(self):
        """Slot 2 used but unresolved — keep the 3MF colour, don't load the archive."""
        db = self._make_db(MagicMock())
        await _apply_spool_colors_to_archive(
            db,
            archive_id=10,
            filament_usage=[
                {"slot_id": 1, "used_g": 10.0},
                {"slot_id": 2, "used_g": 20.0},
            ],
            slot_colors={1: "000000"},
        )
        db.execute.assert_not_awaited()
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_archive_does_not_crash(self):
        """Archive row gone (deleted between completion and reporting)."""
        db = self._make_db(None)
        await _apply_spool_colors_to_archive(
            db,
            archive_id=10,
            filament_usage=[{"slot_id": 1, "used_g": 15.9}],
            slot_colors={1: "000000"},
        )
        db.commit.assert_not_awaited()


class _Session:
    """``async with`` shim over one db double — ``async_session()``'s shape."""

    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc):
        return False


def _another_jobs_live_printer():
    """The live printer as a late terminal finds it: running ANOTHER job at layer 190 of 200."""
    printer_manager = MagicMock()
    printer_manager.get_status.return_value = SimpleNamespace(raw_data={}, layer_num=190, total_layers=200, progress=95)
    return printer_manager


class TestTheSpoolmanChargeFollowsTheTerminalsOwnEvidence:
    """The Spoolman lane of the charge — the same class as the internal ledger's 2026-09-16 → 24
    phantom (~7.2 kg over 32 charges scaled by the next job's live progress): a partial report is
    scaled by the ENDING job's own evidence, and the lane runs on the terminal outcome's basis."""

    @pytest.mark.asyncio
    async def test_a_partial_report_scales_by_the_payloads_layers_never_the_live_printers(self):
        """Layer 40 of 100 of a 100 g plate is 40 g — the live printer's 190/200 would say 95 g."""
        from backend.app.services.spoolman_tracking import _report_partial_usage
        from backend.app.services.usage_tracker import JobEvidence

        tracking = SimpleNamespace(
            archive_id=7,
            filament_usage=[{"slot_id": 1, "used_g": 100.0}],
            layer_usage=None,
            filament_properties=None,
            ams_trays={},
            slot_to_tray=None,
            tray_remain_start={},
        )
        live = _another_jobs_live_printer()
        report = AsyncMock(return_value=1)

        with (
            patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
            patch("backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback", AsyncMock()),
            patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="serial")),
            patch("backend.app.services.spoolman_tracking._report_spool_usage_for_slots", report),
            patch("backend.app.services.printer_manager.printer_manager", live),
        ):
            await _report_partial_usage(1, tracking, JobEvidence(last_layer_num=40, total_layers=100))

        assert report.await_args.args[1] == [(1, 40.0)]
        live.get_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_none_basis_reports_nothing_and_still_retires_the_row(self):
        from backend.app.services import spoolman_tracking

        db = AsyncMock()
        found = MagicMock()
        found.scalar_one_or_none.return_value = SimpleNamespace(archive_id=7)
        db.execute = AsyncMock(side_effect=[found, MagicMock()])
        partial = AsyncMock()

        with patch.object(spoolman_tracking, "_report_partial_usage", partial):
            await spoolman_tracking.cleanup_tracking(1, 7, db, evidence=None)

        partial.assert_not_awaited()
        assert db.execute.await_count == 2  # the lookup, then the delete
        db.commit.assert_awaited_once()

    @pytest.mark.parametrize(
        ("charge", "reports_the_plate", "evidence"),
        [
            pytest.param("full", True, None, id="full_reports_the_whole_plate"),
            pytest.param("partial", False, (40, 100, 20.0), id="partial_hands_over_the_payloads_evidence"),
            pytest.param("none", False, None, id="none_hands_over_nothing"),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_job_phase_runs_the_lane_on_the_outcomes_basis(self, charge, reports_the_plate, evidence):
        from backend.app.services import job_terminal, spoolman_tracking

        payload = {"status": "cancelled", "last_layer_num": 40, "total_layers": 100, "last_progress": 20.0}
        outcome = SimpleNamespace(charge=charge)
        report_usage, cleanup = AsyncMock(), AsyncMock()

        with (
            patch.object(spoolman_tracking, "report_usage", report_usage),
            patch.object(spoolman_tracking, "cleanup_tracking", cleanup),
            patch.object(job_terminal._database, "async_session", lambda: _Session(AsyncMock())),
        ):
            await job_terminal._charge_spoolman(1, payload, outcome, archive_id=7)

        assert report_usage.await_count == (1 if reports_the_plate else 0)
        if reports_the_plate:
            cleanup.assert_not_awaited()
            return
        handed = cleanup.await_args.kwargs["evidence"]
        if evidence is None:
            assert handed is None
        else:
            assert (handed.last_layer_num, handed.total_layers, handed.last_progress) == evidence
