"""Tests for the slot-inventory builder in spool_selection (#1508 + FIFO).

The MQTT ``remain`` field on an AMS tray is the printer firmware's
RFID-tracked value, which is ``-1`` for non-Bambu spools (and even when
set diverges from Bambuddy's inventory). When the user has bound an
inventory spool to an AMS slot, that inventory record's
``label_weight - weight_used`` (or Spoolman's ``remaining_weight``) is
the authoritative remaining-weight signal, and ``COALESCE(first_loaded_at,
created_at)`` (Spoolman: ``first_used``) is the FIFO ordinal. These tests
verify ``build_slot_inventory`` surfaces both, keyed by ``global_tray_id``.

``PrintScheduler._build_inventory_remain_overrides`` is now a thin delegate
that projects the remaining-grams side of this map; a couple of cases pin
that delegate to guard the external key/shape it still returns.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.print_scheduler import PrintScheduler
from backend.app.services.spool_selection import SlotInventory, build_slot_inventory


@pytest.fixture
def scheduler():
    return PrintScheduler()


def _make_async_session_returning(rows: list):
    """Build a stub AsyncSession whose .execute() returns an object whose
    .all() (and .scalars().all()) yield ``rows``."""
    result = MagicMock()
    result.all.return_value = rows
    scalars = MagicMock()
    scalars.all.return_value = rows
    result.scalars.return_value = scalars
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _spool(*, label_weight, weight_used, loaded_at=None, first_loaded_at=None, created_at=None, spent_at=None):
    """Internal-mode spool stub with the attributes build_slot_inventory reads."""
    return SimpleNamespace(
        label_weight=label_weight,
        weight_used=weight_used,
        loaded_at=loaded_at,
        first_loaded_at=first_loaded_at,
        created_at=created_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
        feed_fault_at=None,
        spent_at=spent_at,
    )


class TestInternalInventoryOverrides:
    @pytest.mark.asyncio
    async def test_returns_remaining_grams_for_bound_slots(self):
        """Two slots bound; both come back keyed by global_tray_id with the
        correct ``label_weight - weight_used`` in grams (reporter scenario #1508:
        slot 1 has a 950 g clone, slot 4 a 50 g original)."""
        rows = [
            SimpleNamespace(ams_id=0, tray_id=0, spool=_spool(label_weight=1000, weight_used=50)),
            SimpleNamespace(ams_id=0, tray_id=3, spool=_spool(label_weight=1000, weight_used=950)),
        ]
        loaded = [
            {"ams_id": 0, "tray_id": 0, "global_tray_id": 0, "is_external": False},
            {"ams_id": 0, "tray_id": 3, "global_tray_id": 3, "is_external": False},
        ]
        db = _make_async_session_returning(rows)
        with patch("backend.app.services.spool_selection._is_spoolman_mode", new=AsyncMock(return_value=False)):
            out = await build_slot_inventory(db, printer_id=1, loaded=loaded)
        assert out[0].remaining_g == 950.0
        assert out[3].remaining_g == 50.0

    @pytest.mark.asyncio
    async def test_spent_and_feed_fault_flags_propagate(self):
        """``SlotInventory.spent`` mirrors ``spool.spent_at`` (and out_of_rotation
        mirrors ``feed_fault_at``) so the matcher can hard-exclude either — a
        spent or jammed spool must never start a print."""
        spent_spool = _spool(label_weight=1000, weight_used=0, spent_at=datetime(2026, 5, 1, tzinfo=timezone.utc))
        live_spool = _spool(label_weight=1000, weight_used=0)
        rows = [
            SimpleNamespace(ams_id=0, tray_id=0, spool=spent_spool),
            SimpleNamespace(ams_id=0, tray_id=1, spool=live_spool),
        ]
        loaded = [
            {"ams_id": 0, "tray_id": 0, "global_tray_id": 0, "is_external": False},
            {"ams_id": 0, "tray_id": 1, "global_tray_id": 1, "is_external": False},
        ]
        db = _make_async_session_returning(rows)
        with patch("backend.app.services.spool_selection._is_spoolman_mode", new=AsyncMock(return_value=False)):
            out = await build_slot_inventory(db, printer_id=1, loaded=loaded)
        assert out[0].spent is True and out[0].out_of_rotation is False
        assert out[1].spent is False and out[1].out_of_rotation is False

    @pytest.mark.asyncio
    async def test_first_loaded_ordinal_prefers_first_loaded_at(self):
        """``first_loaded_at`` wins over ``created_at`` for the FIFO ordinal;
        when NULL the builder falls back to ``created_at``."""
        first = datetime(2026, 2, 1, tzinfo=timezone.utc)
        created = datetime(2026, 3, 1, tzinfo=timezone.utc)
        rows = [
            SimpleNamespace(
                ams_id=0,
                tray_id=0,
                spool=_spool(label_weight=1000, weight_used=0, first_loaded_at=first, created_at=created),
            ),
            SimpleNamespace(
                ams_id=0,
                tray_id=1,
                spool=_spool(label_weight=1000, weight_used=0, first_loaded_at=None, created_at=created),
            ),
        ]
        loaded = [
            {"ams_id": 0, "tray_id": 0, "global_tray_id": 0, "is_external": False},
            {"ams_id": 0, "tray_id": 1, "global_tray_id": 1, "is_external": False},
        ]
        db = _make_async_session_returning(rows)
        with patch("backend.app.services.spool_selection._is_spoolman_mode", new=AsyncMock(return_value=False)):
            out = await build_slot_inventory(db, printer_id=1, loaded=loaded)
        assert out[0].first_loaded_ord == first.timestamp()
        assert out[1].first_loaded_ord == created.timestamp()
        # ord_src names the source that won: first_loaded_at then created_at.
        assert out[0].ord_src == "first_loaded_at"
        assert out[1].ord_src == "created_at"

    @pytest.mark.asyncio
    async def test_loaded_at_beats_first_loaded_at(self):
        """The re-stampable ``loaded_at`` (a real re-seat) wins the ordinal over the
        write-once ``first_loaded_at`` history — the whole 006-H2S FIFO fix: a fresh
        roll re-seated into a slot whose ledger row is OLD must sort by when the roll
        became seated, not by the stale row age. ord_src reflects the winner."""
        loaded_ts = datetime(2026, 7, 21, tzinfo=timezone.utc)  # re-seated recently
        first_ts = datetime(2026, 2, 1, tzinfo=timezone.utc)  # old ledger row age
        rows = [
            SimpleNamespace(
                ams_id=0,
                tray_id=0,
                spool=_spool(label_weight=1000, weight_used=0, loaded_at=loaded_ts, first_loaded_at=first_ts),
            ),
        ]
        loaded = [{"ams_id": 0, "tray_id": 0, "global_tray_id": 0, "is_external": False}]
        db = _make_async_session_returning(rows)
        with patch("backend.app.services.spool_selection._is_spoolman_mode", new=AsyncMock(return_value=False)):
            out = await build_slot_inventory(db, printer_id=1, loaded=loaded)
        assert out[0].first_loaded_ord == loaded_ts.timestamp()
        assert out[0].ord_src == "loaded_at"

    @pytest.mark.asyncio
    async def test_ordinal_three_step_fallback_chain(self):
        """loaded_at → first_loaded_at → created_at, in that precedence, each tagged
        by ord_src so the trace tells a genuine reseat stamp from a stale fallback."""
        created = datetime(2026, 1, 1, tzinfo=timezone.utc)
        first = datetime(2026, 2, 1, tzinfo=timezone.utc)
        loaded_ts = datetime(2026, 3, 1, tzinfo=timezone.utc)
        rows = [
            SimpleNamespace(  # all three present → loaded_at
                ams_id=0,
                tray_id=0,
                spool=_spool(
                    label_weight=1000, weight_used=0, loaded_at=loaded_ts, first_loaded_at=first, created_at=created
                ),
            ),
            SimpleNamespace(  # no loaded_at → first_loaded_at
                ams_id=0,
                tray_id=1,
                spool=_spool(
                    label_weight=1000, weight_used=0, loaded_at=None, first_loaded_at=first, created_at=created
                ),
            ),
            SimpleNamespace(  # neither → created_at
                ams_id=0,
                tray_id=2,
                spool=_spool(
                    label_weight=1000, weight_used=0, loaded_at=None, first_loaded_at=None, created_at=created
                ),
            ),
        ]
        loaded = [
            {"ams_id": 0, "tray_id": 0, "global_tray_id": 0, "is_external": False},
            {"ams_id": 0, "tray_id": 1, "global_tray_id": 1, "is_external": False},
            {"ams_id": 0, "tray_id": 2, "global_tray_id": 2, "is_external": False},
        ]
        db = _make_async_session_returning(rows)
        with patch("backend.app.services.spool_selection._is_spoolman_mode", new=AsyncMock(return_value=False)):
            out = await build_slot_inventory(db, printer_id=1, loaded=loaded)
        assert (out[0].first_loaded_ord, out[0].ord_src) == (loaded_ts.timestamp(), "loaded_at")
        assert (out[1].first_loaded_ord, out[1].ord_src) == (first.timestamp(), "first_loaded_at")
        assert (out[2].first_loaded_ord, out[2].ord_src) == (created.timestamp(), "created_at")

    @pytest.mark.asyncio
    async def test_skips_external_slots(self):
        """VT / external slots are tracked separately — never assigned an
        inventory value even if an assignment row somehow exists."""
        loaded = [{"ams_id": -1, "tray_id": 0, "global_tray_id": 254, "is_external": True}]
        db = _make_async_session_returning([])
        with patch("backend.app.services.spool_selection._is_spoolman_mode", new=AsyncMock(return_value=False)):
            out = await build_slot_inventory(db, printer_id=1, loaded=loaded)
        db.execute.assert_not_called()
        assert out == {}

    @pytest.mark.asyncio
    async def test_empty_loaded_returns_empty(self):
        """No loaded filaments → no inventory."""
        db = _make_async_session_returning([])
        with patch("backend.app.services.spool_selection._is_spoolman_mode", new=AsyncMock(return_value=False)):
            out = await build_slot_inventory(db, printer_id=1, loaded=[])
        assert out == {}
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_negative_remaining_clamped_to_zero(self):
        """An over-consumed spool clamps remaining to 0, not negative."""
        rows = [SimpleNamespace(ams_id=0, tray_id=0, spool=_spool(label_weight=1000, weight_used=1100))]
        loaded = [{"ams_id": 0, "tray_id": 0, "global_tray_id": 0, "is_external": False}]
        db = _make_async_session_returning(rows)
        with patch("backend.app.services.spool_selection._is_spoolman_mode", new=AsyncMock(return_value=False)):
            out = await build_slot_inventory(db, printer_id=1, loaded=loaded)
        assert out[0].remaining_g == 0.0

    @pytest.mark.asyncio
    async def test_slot_without_binding_absent(self):
        """A loaded slot with no inventory binding is absent from the map —
        the sort falls back to MQTT ``remain`` for it."""
        rows = [SimpleNamespace(ams_id=0, tray_id=0, spool=_spool(label_weight=1000, weight_used=100))]
        loaded = [
            {"ams_id": 0, "tray_id": 0, "global_tray_id": 0, "is_external": False},
            {"ams_id": 0, "tray_id": 1, "global_tray_id": 1, "is_external": False},
        ]
        db = _make_async_session_returning(rows)
        with patch("backend.app.services.spool_selection._is_spoolman_mode", new=AsyncMock(return_value=False)):
            out = await build_slot_inventory(db, printer_id=1, loaded=loaded)
        assert out[0].remaining_g == 900.0
        assert 1 not in out

    @pytest.mark.asyncio
    async def test_delegate_projects_remaining_grams(self, scheduler):
        """The retained ``_build_inventory_remain_overrides`` delegate projects
        the remaining-grams side, preserving its external ``{gtid: grams}`` shape."""
        rows = [
            SimpleNamespace(ams_id=0, tray_id=0, spool=_spool(label_weight=1000, weight_used=50)),
            SimpleNamespace(ams_id=0, tray_id=3, spool=_spool(label_weight=1000, weight_used=950)),
        ]
        loaded = [
            {"ams_id": 0, "tray_id": 0, "global_tray_id": 0, "is_external": False},
            {"ams_id": 0, "tray_id": 3, "global_tray_id": 3, "is_external": False},
        ]
        db = _make_async_session_returning(rows)
        with patch("backend.app.services.spool_selection._is_spoolman_mode", new=AsyncMock(return_value=False)):
            out = await scheduler._build_inventory_remain_overrides(db, printer_id=1, loaded=loaded)
        assert out == {0: 950.0, 3: 50.0}


class TestSpoolmanModeOverrides:
    @pytest.mark.asyncio
    async def test_spoolman_remaining_and_first_used_used_when_available(self):
        """Spoolman mode: each bound slot's spoolman_spool_id is fetched once,
        yielding both remaining grams and the first_used ordinal."""
        rows = [
            SimpleNamespace(printer_id=1, ams_id=0, tray_id=0, spoolman_spool_id=42),
            SimpleNamespace(printer_id=1, ams_id=0, tray_id=2, spoolman_spool_id=99),
        ]
        loaded = [
            {"ams_id": 0, "tray_id": 0, "global_tray_id": 0, "is_external": False},
            {"ams_id": 0, "tray_id": 2, "global_tray_id": 2, "is_external": False},
        ]
        db = _make_async_session_returning(rows)

        async def _fake_fetch(spool_id: int):
            return {42: (720.0, 1000.0), 99: (80.0, 2000.0)}[spool_id]

        with (
            patch("backend.app.services.spool_selection._is_spoolman_mode", new=AsyncMock(return_value=True)),
            patch("backend.app.services.spool_selection._fetch_spoolman_slot", new=AsyncMock(side_effect=_fake_fetch)),
        ):
            out = await build_slot_inventory(db, printer_id=1, loaded=loaded)
        assert out[0].remaining_g == 720.0
        assert out[0].first_loaded_ord == 1000.0
        assert out[0].ord_src == "first_used"
        assert out[2].remaining_g == 80.0


class TestDtToEpoch:
    """``_dt_to_epoch`` pins a naive datetime to UTC so a naive stamp and its aware-UTC
    twin yield the SAME absolute epoch — the latent clock hazard 006-H2S flagged (SQLite
    hands stamps back naive; ``.timestamp()`` would otherwise read them as local)."""

    def test_naive_utc_equals_aware_utc_twin(self):
        from backend.app.services.spool_selection import _dt_to_epoch

        naive = datetime(2026, 5, 1, 12, 0, 0)
        aware = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert _dt_to_epoch(naive) == _dt_to_epoch(aware)
        assert _dt_to_epoch(aware) == aware.timestamp()

    def test_none_is_none(self):
        from backend.app.services.spool_selection import _dt_to_epoch

        assert _dt_to_epoch(None) is None

    @pytest.mark.asyncio
    async def test_spoolman_unreachable_skips_silently(self):
        """If Spoolman is unreachable for one spool, ``_fetch_spoolman_slot``
        returns (None, None) and that slot is omitted."""
        rows = [
            SimpleNamespace(printer_id=1, ams_id=0, tray_id=0, spoolman_spool_id=42),
            SimpleNamespace(printer_id=1, ams_id=0, tray_id=1, spoolman_spool_id=99),
        ]
        loaded = [
            {"ams_id": 0, "tray_id": 0, "global_tray_id": 0, "is_external": False},
            {"ams_id": 0, "tray_id": 1, "global_tray_id": 1, "is_external": False},
        ]
        db = _make_async_session_returning(rows)

        async def _fake_fetch(spool_id: int):
            return (500.0, None) if spool_id == 42 else (None, None)

        with (
            patch("backend.app.services.spool_selection._is_spoolman_mode", new=AsyncMock(return_value=True)),
            patch("backend.app.services.spool_selection._fetch_spoolman_slot", new=AsyncMock(side_effect=_fake_fetch)),
        ):
            out = await build_slot_inventory(db, printer_id=1, loaded=loaded)
        assert out[0].remaining_g == 500.0
        assert 1 not in out
        # Sanity: the retained delegate omits the None-remaining slot too.
        assert isinstance(out[0], SlotInventory)
