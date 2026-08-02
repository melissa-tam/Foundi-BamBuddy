"""Unit tests for POST /inventory/sync-ams-weights.

Two layers: the pure weight arithmetic / remain validation, and the ROUTE's three
data-safety guards (W4, 2026-08-02) exercised end to end against a seeded DB and a
faked live status — presence, remain 1..100, and the per-printer mid-print gate.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.app.api.routes.inventory import _find_tray_in_ams_data, sync_weights_from_ams
from backend.app.models.archive import PrintArchive
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment


def _calc_weight_used(label_weight: int | None, remain: int) -> float:
    """Reproduce the weight calculation from sync_weights_from_ams."""
    lw = label_weight or 1000
    return round(lw * (100 - remain) / 100.0, 1)


def _is_valid_remain(remain_raw) -> tuple[bool, int]:
    """Reproduce the remain% validation from sync_weights_from_ams.

    Returns (is_valid, parsed_value).  parsed_value is only meaningful
    when is_valid is True.
    """
    if remain_raw is None:
        return False, 0
    try:
        val = int(remain_raw)
    except (TypeError, ValueError):
        return False, 0
    if val < 1 or val > 100:
        return False, val
    return True, val


class TestWeightCalculation:
    """Test the weight_used = label_weight * (100 - remain) / 100 formula."""

    def test_remain_100_means_no_usage(self):
        """A full spool (remain=100) should have weight_used=0."""
        assert _calc_weight_used(1000, 100) == 0.0

    def test_remain_50_with_1000g_spool(self):
        """Half-used 1000g spool should have weight_used=500."""
        assert _calc_weight_used(1000, 50) == 500.0

    def test_remain_0_would_write_the_whole_label(self):
        """Why remain=0 is REJECTED upstream: the formula turns it into
        weight_used = label_weight, i.e. "this roll is gone". A cleared tray reports
        remain=0, so accepting it wrote every stale-bound roll on the fleet to empty.
        The arithmetic is fine; the input is not — see TestRouteGuards."""
        assert _calc_weight_used(1000, 0) == 1000.0

    def test_respects_label_weight_500g(self):
        """500g spool at remain=50 should have weight_used=250."""
        assert _calc_weight_used(500, 50) == 250.0

    def test_respects_label_weight_250g(self):
        """250g spool at remain=75 should have weight_used=62.5."""
        assert _calc_weight_used(250, 75) == 62.5

    def test_none_label_weight_defaults_to_1000(self):
        """When label_weight is None, it defaults to 1000g."""
        assert _calc_weight_used(None, 50) == 500.0

    def test_result_is_rounded_to_one_decimal(self):
        """Weight used should be rounded to 1 decimal place.

        For a 1000g spool at remain=33, weight_used = 1000 * 67 / 100 = 670.0
        """
        assert _calc_weight_used(1000, 33) == 670.0

    def test_odd_fraction_rounds_correctly(self):
        """750g spool at remain=33 → 750 * 67/100 = 502.5."""
        assert _calc_weight_used(750, 33) == 502.5

    def test_small_spool_small_remain(self):
        """200g spool at remain=1 → 200 * 99/100 = 198.0."""
        assert _calc_weight_used(200, 1) == 198.0


class TestRemainValidation:
    """Test the remain% bounds and type validation."""

    def test_remain_minus_1_is_invalid(self):
        """remain=-1 (firmware 'unknown') should be skipped."""
        valid, _ = _is_valid_remain(-1)
        assert valid is False

    def test_remain_101_is_invalid(self):
        """remain=101 (out of range) should be skipped."""
        valid, _ = _is_valid_remain(101)
        assert valid is False

    def test_remain_negative_large_is_invalid(self):
        """Large negative remain values should be skipped."""
        valid, _ = _is_valid_remain(-50)
        assert valid is False

    def test_remain_200_is_invalid(self):
        """remain=200 should be skipped."""
        valid, _ = _is_valid_remain(200)
        assert valid is False

    def test_remain_none_is_invalid(self):
        """remain=None (missing from tray data) should be skipped."""
        valid, _ = _is_valid_remain(None)
        assert valid is False

    def test_remain_non_numeric_string_is_invalid(self):
        """Non-numeric string remain should be skipped."""
        valid, _ = _is_valid_remain("abc")
        assert valid is False

    def test_remain_0_is_invalid(self):
        """remain=0 is firmware's "no reading" (and what a cleared tray reports), not
        a measured-empty roll — the endpoint now rejects it exactly like the on-push
        sync always has."""
        valid, _ = _is_valid_remain(0)
        assert valid is False

    def test_remain_100_is_valid(self):
        """remain=100 should be valid."""
        valid, val = _is_valid_remain(100)
        assert valid is True
        assert val == 100

    def test_remain_50_is_valid(self):
        """remain=50 should be valid."""
        valid, val = _is_valid_remain(50)
        assert valid is True
        assert val == 50

    def test_remain_string_number_is_valid(self):
        """Numeric string remain (e.g. '75') should be parsed as int."""
        valid, val = _is_valid_remain("75")
        assert valid is True
        assert val == 75


class TestFindTrayInAmsData:
    """Test the _find_tray_in_ams_data helper used by the sync endpoint."""

    def test_finds_matching_tray(self):
        """Should return the matching tray dict."""
        ams_data = [
            {
                "id": 0,
                "tray": [
                    {"id": 0, "remain": 80},
                    {"id": 1, "remain": 50},
                ],
            },
        ]
        tray = _find_tray_in_ams_data(ams_data, ams_id=0, tray_id=1)
        assert tray is not None
        assert tray["remain"] == 50

    def test_returns_none_for_missing_ams_unit(self):
        """Should return None when the AMS unit ID is not found."""
        ams_data = [{"id": 0, "tray": [{"id": 0, "remain": 80}]}]
        assert _find_tray_in_ams_data(ams_data, ams_id=1, tray_id=0) is None

    def test_returns_none_for_missing_tray(self):
        """Should return None when the tray ID is not found."""
        ams_data = [{"id": 0, "tray": [{"id": 0, "remain": 80}]}]
        assert _find_tray_in_ams_data(ams_data, ams_id=0, tray_id=3) is None

    def test_returns_none_for_empty_data(self):
        """Should return None for empty AMS data."""
        assert _find_tray_in_ams_data([], ams_id=0, tray_id=0) is None

    def test_returns_none_for_none_data(self):
        """Should return None for None AMS data."""
        assert _find_tray_in_ams_data(None, ams_id=0, tray_id=0) is None

    def test_multi_ams_unit_lookup(self):
        """Should find trays across multiple AMS units."""
        ams_data = [
            {"id": 0, "tray": [{"id": 0, "remain": 80}]},
            {"id": 1, "tray": [{"id": 2, "remain": 30}]},
        ]
        tray = _find_tray_in_ams_data(ams_data, ams_id=1, tray_id=2)
        assert tray is not None
        assert tray["remain"] == 30

    def test_ams_ht_high_id(self):
        """Should find trays in AMS-HT units (id >= 128)."""
        ams_data = [{"id": 128, "tray": [{"id": 0, "remain": 65}]}]
        tray = _find_tray_in_ams_data(ams_data, ams_id=128, tray_id=0)
        assert tray is not None
        assert tray["remain"] == 65


class TestSyncSkipLogic:
    """Test combinations that exercise the sync/skip decision path."""

    def test_same_value_is_skipped(self):
        """When old weight_used matches new, the spool is skipped (no DB write)."""
        # Simulating the endpoint logic: if round(old_used, 1) == new_used → skip
        label_weight = 1000
        remain = 50
        new_used = _calc_weight_used(label_weight, remain)
        old_used = 500.0  # Already matches
        assert round(old_used, 1) == new_used  # → would be skipped

    def test_different_value_is_synced(self):
        """When old weight_used differs from new, the spool is synced."""
        label_weight = 1000
        remain = 50
        new_used = _calc_weight_used(label_weight, remain)
        old_used = 300.0  # Different
        assert round(old_used, 1) != new_used  # → would be synced

    def test_none_old_used_treated_as_zero(self):
        """When old weight_used is None (new spool), it defaults to 0."""
        old_used = None
        effective_old = old_used or 0
        new_used = _calc_weight_used(1000, 80)  # 200.0
        assert effective_old == 0
        assert round(effective_old, 1) != new_used  # → would be synced

    def test_remain_0_never_reaches_calc(self):
        """remain=0 fails validation before the weight calculation — the endpoint
        now agrees with on_ams_change instead of being the 1..100 outlier."""
        valid, _ = _is_valid_remain(0)
        assert valid is False

    def test_remain_minus_1_never_reaches_calc(self):
        """remain=-1 fails validation before weight calculation."""
        valid, _ = _is_valid_remain(-1)
        assert valid is False
        # The endpoint would skip += 1 and continue

    def test_remain_101_never_reaches_calc(self):
        """remain=101 fails validation before weight calculation."""
        valid, _ = _is_valid_remain(101)
        assert valid is False


# ── Route-level guards (W4) ──────────────────────────────────────────────────


class _FakeManager:
    """Minimal printer_manager stand-in: get_status only."""

    def __init__(self, states: dict):
        self._states = states

    def get_status(self, printer_id: int):
        return self._states.get(printer_id)


def _status(trays: list[dict], *, ams_id: int = 0, gcode_state: str = "IDLE"):
    return SimpleNamespace(state=gcode_state, raw_data={"ams": [{"id": ams_id, "tray": trays}]})


def _loaded_tray(tray_id: int = 0, *, remain: int = 50, state: int = 11, tray_type: str = "PETG"):
    return {"id": tray_id, "state": state, "tray_type": tray_type, "remain": remain}


def _cleared_tray(tray_id: int = 0):
    """The verified prod cleared-tray shape: state 9 + asserted-empty type, and the
    remain=0 the firmware reports alongside it."""
    return {"id": tray_id, "state": 9, "tray_type": "", "remain": 0}


async def _seed_bound_spool(db, printer_id: int, *, weight_used: float = 400.0, ams_id=0, tray_id=0) -> Spool:
    spool = Spool(material="PETG", rgba="000000FF", label_weight=1000, weight_used=weight_used)
    db.add(spool)
    await db.flush()
    db.add(SpoolAssignment(spool_id=spool.id, printer_id=printer_id, ams_id=ams_id, tray_id=tray_id))
    await db.commit()
    return spool


async def _used(db, spool_id: int) -> float:
    db.expunge_all()
    return (await db.execute(select(Spool.weight_used).where(Spool.id == spool_id))).scalar_one()


@pytest.mark.asyncio
class TestRouteGuards:
    """The three guards that stop this manual recovery tool destroying ledgers."""

    async def test_happy_path_still_syncs(self, db_session, printer_factory, monkeypatch):
        """A seated tray on an idle printer syncs both directions (that is the whole
        point of the tool — it bypasses the increase-only guard)."""
        printer = await printer_factory()
        spool = await _seed_bound_spool(db_session, printer.id, weight_used=400.0)
        monkeypatch.setattr(
            "backend.app.services.printer_manager.printer_manager",
            _FakeManager({printer.id: _status([_loaded_tray(remain=90)])}),
        )

        result = await sync_weights_from_ams(db=db_session, _=None)

        assert result == {"synced": 1, "skipped": 0}
        assert await _used(db_session, spool.id) == 100.0  # wrote DOWN from 400

    async def test_cleared_tray_is_skipped(self, db_session, printer_factory, monkeypatch):
        """PRESENCE GUARD — five prod slots hold a stale binding on a physically
        EMPTY tray. Its remain% describes no roll, so the row is left alone."""
        printer = await printer_factory()
        spool = await _seed_bound_spool(db_session, printer.id, weight_used=932.0)
        monkeypatch.setattr(
            "backend.app.services.printer_manager.printer_manager",
            _FakeManager({printer.id: _status([_cleared_tray()])}),
        )

        result = await sync_weights_from_ams(db=db_session, _=None)

        assert result == {"synced": 0, "skipped": 1}
        assert await _used(db_session, spool.id) == 932.0  # untouched

    async def test_remain_zero_on_a_present_tray_is_skipped(self, db_session, printer_factory, monkeypatch):
        """REMAIN GUARD — the data-destruction pin. remain=0 used to pass here and
        compute weight_used = label_weight; even on a tray that reads PRESENT it is
        firmware's "no reading", never a measurement."""
        printer = await printer_factory()
        spool = await _seed_bound_spool(db_session, printer.id, weight_used=120.0)
        monkeypatch.setattr(
            "backend.app.services.printer_manager.printer_manager",
            _FakeManager({printer.id: _status([_loaded_tray(remain=0)])}),
        )

        result = await sync_weights_from_ams(db=db_session, _=None)

        assert result == {"synced": 0, "skipped": 1}
        assert await _used(db_session, spool.id) == 120.0  # NOT 1000.0

    @pytest.mark.parametrize("gcode_state", ["RUNNING", "PAUSE", "PREPARE", "SLICING", "UNKNOWN", ""])
    async def test_active_printer_is_skipped(self, db_session, printer_factory, monkeypatch, gcode_state):
        """MID-PRINT GUARD — the usage tracker owns a live print's deduction; folding
        the integer remain% in on top double-counts (#880). An unknown state fails
        closed for the same reason."""
        printer = await printer_factory()
        spool = await _seed_bound_spool(db_session, printer.id, weight_used=400.0)
        monkeypatch.setattr(
            "backend.app.services.printer_manager.printer_manager",
            _FakeManager({printer.id: _status([_loaded_tray(remain=90)], gcode_state=gcode_state)}),
        )

        result = await sync_weights_from_ams(db=db_session, _=None)

        assert result == {"synced": 0, "skipped": 1}
        assert await _used(db_session, spool.id) == 400.0

    async def test_still_printing_archive_blocks_an_idle_looking_printer(
        self, db_session, printer_factory, monkeypatch
    ):
        """The gate's second, DURABLE leg: a live state that lags the DB. An archive
        still marked ``printing`` blocks the sync even on an IDLE-reporting printer."""
        printer = await printer_factory()
        spool = await _seed_bound_spool(db_session, printer.id, weight_used=400.0)
        db_session.add(
            PrintArchive(
                printer_id=printer.id,
                filename="job.3mf",
                file_path="/tmp/job.3mf",
                file_size=1,
                print_name="job",
                status="printing",
                started_at=datetime.utcnow(),
            )
        )
        await db_session.commit()
        monkeypatch.setattr(
            "backend.app.services.printer_manager.printer_manager",
            _FakeManager({printer.id: _status([_loaded_tray(remain=90)])}),
        )

        result = await sync_weights_from_ams(db=db_session, _=None)

        assert result == {"synced": 0, "skipped": 1}
        assert await _used(db_session, spool.id) == 400.0

    async def test_unknown_presence_fails_open(self, db_session, printer_factory, monkeypatch):
        """Tri-state discipline: the A1-family always-state-3 dialect reads UNKNOWN,
        never EMPTY, so its slots still sync."""
        printer = await printer_factory()
        spool = await _seed_bound_spool(db_session, printer.id, weight_used=400.0)
        monkeypatch.setattr(
            "backend.app.services.printer_manager.printer_manager",
            _FakeManager({printer.id: _status([_loaded_tray(remain=90, state=3)])}),
        )

        result = await sync_weights_from_ams(db=db_session, _=None)

        assert result == {"synced": 1, "skipped": 0}
        assert await _used(db_session, spool.id) == 100.0
