"""Low-spool staging release tests (Phase 4.2).

``release_filament_staged`` re-runs the deficit check for SYSTEM-staged items
(``manual_start`` + ``filament_short``) and un-stages ONLY the ones whose
deficit actually cleared; still-short items stay staged (no bounce). The AMS
hook is debounced by a tray-signature hash (first push seeds without firing)
and pre-checks for staged FARM items before paying the recompute. FK
enforcement is off in the test engine.
"""

from unittest.mock import AsyncMock

import pytest

from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import farm_staging

# asyncio_mode = "auto" (pyproject) picks up the async tests; the signature
# tests below are plain sync functions, so no module-level asyncio mark.


@pytest.fixture(autouse=True)
def _clean_state():
    farm_staging._reset_state()
    yield
    farm_staging._reset_state()


async def _add_staged(db, *, printer_id, batch_id=None, waiting_reason=None, pos=1):
    it = PrintQueueItem(
        batch_id=batch_id,
        printer_id=printer_id,
        status="pending",
        manual_start=True,
        filament_short=True,
        waiting_reason=waiting_reason,
        plate_id=1,
        position=pos,
    )
    db.add(it)
    await db.commit()
    await db.refresh(it)
    return it


def _patch_deficit(monkeypatch, fn):
    monkeypatch.setattr(farm_staging, "compute_deficit_for_queue_item", fn)


class TestReleaseFilamentStaged:
    async def test_releases_when_deficit_cleared(self, db_session, monkeypatch):
        item = await _add_staged(db_session, printer_id=1, waiting_reason="filament_short")
        _patch_deficit(monkeypatch, AsyncMock(return_value=[]))
        released = await farm_staging.release_filament_staged(db_session)
        assert released == 1
        await db_session.refresh(item)
        assert item.manual_start is False
        assert item.filament_short is False
        assert item.waiting_reason is None

    async def test_leaves_still_short_item_staged(self, db_session, monkeypatch):
        item = await _add_staged(db_session, printer_id=1)
        _patch_deficit(monkeypatch, AsyncMock(return_value=[{"slot_id": 1}]))
        released = await farm_staging.release_filament_staged(db_session)
        assert released == 0
        await db_session.refresh(item)
        assert item.manual_start is True
        assert item.filament_short is True

    async def test_preserves_unrelated_waiting_reason(self, db_session, monkeypatch):
        item = await _add_staged(db_session, printer_id=1, waiting_reason="no idle printer")
        _patch_deficit(monkeypatch, AsyncMock(return_value=[]))
        assert await farm_staging.release_filament_staged(db_session) == 1
        await db_session.refresh(item)
        assert item.waiting_reason == "no idle printer"

    async def test_printer_scoped_release(self, db_session, monkeypatch):
        on_target = await _add_staged(db_session, printer_id=1, pos=1)
        elsewhere = await _add_staged(db_session, printer_id=2, pos=2)
        _patch_deficit(monkeypatch, AsyncMock(return_value=[]))
        released = await farm_staging.release_filament_staged(db_session, printer_id=1)
        assert released == 1
        await db_session.refresh(on_target)
        await db_session.refresh(elsewhere)
        assert on_target.manual_start is False
        assert elsewhere.manual_start is True  # untouched — other printer

    async def test_scoped_release_includes_unpinned_items(self, db_session, monkeypatch):
        """UNPINNED all-short items (printer_id NULL, staged by the model-based
        candidate loop) are released by ANY printer-scoped pass — a spool swap on
        one printer re-opens the fleet-wide search. Other pinned printers stay put."""
        unpinned = await _add_staged(db_session, printer_id=None, pos=1)
        on_two = await _add_staged(db_session, printer_id=2, pos=2)
        _patch_deficit(monkeypatch, AsyncMock(return_value=[]))
        released = await farm_staging.release_filament_staged(db_session, printer_id=1)
        assert released == 1  # the unpinned item, even though it targets no printer
        await db_session.refresh(unpinned)
        await db_session.refresh(on_two)
        assert unpinned.manual_start is False
        assert on_two.manual_start is True  # pinned to printer 2 — untouched

    async def test_deficit_failure_leaves_item_staged(self, db_session, monkeypatch):
        item = await _add_staged(db_session, printer_id=1)
        _patch_deficit(monkeypatch, AsyncMock(side_effect=RuntimeError("spoolman down")))
        released = await farm_staging.release_filament_staged(db_session)
        assert released == 0  # fail-safe: unknown spool state never releases
        await db_session.refresh(item)
        assert item.manual_start is True

    async def test_operator_staged_rows_untouched(self, db_session, monkeypatch):
        # manual_start WITHOUT filament_short = operator staging — never auto-released.
        it = PrintQueueItem(printer_id=1, status="pending", manual_start=True, filament_short=False, position=1)
        db_session.add(it)
        await db_session.commit()
        spy = AsyncMock(return_value=[])
        _patch_deficit(monkeypatch, spy)
        assert await farm_staging.release_filament_staged(db_session) == 0
        spy.assert_not_awaited()


def _patch_start_rule(monkeypatch, blocks: bool):
    """Force the start-spool floor re-check to a fixed verdict."""
    monkeypatch.setattr(
        farm_staging.spool_selection,
        "start_rule_blocks_item",
        AsyncMock(return_value=blocks),
    )


class TestReleaseStartRuleGate:
    """The release path also honours the minimum-start floor: a pinned item with
    an empty deficit but a below-floor starting spool must NOT release (it would
    re-stage next tick, bouncing)."""

    async def test_keeps_still_start_blocked_item_staged(self, db_session, monkeypatch):
        item = await _add_staged(
            db_session, printer_id=1, waiting_reason=farm_staging.spool_selection.WAITING_REASON_START_MIN
        )
        _patch_deficit(monkeypatch, AsyncMock(return_value=[]))  # deficit clear...
        _patch_start_rule(monkeypatch, blocks=True)  # ...but start floor still blocks
        released = await farm_staging.release_filament_staged(db_session)
        assert released == 0
        await db_session.refresh(item)
        assert item.manual_start is True
        assert item.filament_short is True
        assert item.waiting_reason == farm_staging.spool_selection.WAITING_REASON_START_MIN

    async def test_releases_once_start_rule_clears(self, db_session, monkeypatch):
        item = await _add_staged(
            db_session, printer_id=1, waiting_reason=farm_staging.spool_selection.WAITING_REASON_START_MIN
        )
        _patch_deficit(monkeypatch, AsyncMock(return_value=[]))
        _patch_start_rule(monkeypatch, blocks=False)  # floor cleared (spool topped up)
        released = await farm_staging.release_filament_staged(db_session)
        assert released == 1
        await db_session.refresh(item)
        assert item.manual_start is False
        assert item.filament_short is False
        # The new start-min waiting reason is cleared alongside filament_short.
        assert item.waiting_reason is None

    async def test_start_rule_failure_leaves_item_staged(self, db_session, monkeypatch):
        item = await _add_staged(
            db_session, printer_id=1, waiting_reason=farm_staging.spool_selection.WAITING_REASON_START_MIN
        )
        _patch_deficit(monkeypatch, AsyncMock(return_value=[]))
        monkeypatch.setattr(
            farm_staging.spool_selection,
            "start_rule_blocks_item",
            AsyncMock(side_effect=RuntimeError("status unavailable")),
        )
        released = await farm_staging.release_filament_staged(db_session)
        assert released == 0  # fail-safe: unknown state never releases
        await db_session.refresh(item)
        assert item.manual_start is True


class TestTraySignature:
    def test_changes_on_spool_identity_fields(self):
        base = [{"id": 0, "tray": [{"id": 0, "tray_type": "PETG", "remain": 80, "tray_uuid": "AA"}]}]
        swapped = [{"id": 0, "tray": [{"id": 0, "tray_type": "PETG", "remain": 100, "tray_uuid": "BB"}]}]
        assert farm_staging.compute_tray_signature(base) != farm_staging.compute_tray_signature(swapped)

    def test_ignores_volatile_telemetry(self):
        a = [{"id": 0, "humidity": 3, "tray": [{"id": 0, "tray_type": "PETG", "remain": 80, "tray_uuid": "AA"}]}]
        b = [{"id": 0, "humidity": 4, "tray": [{"id": 0, "tray_type": "PETG", "remain": 80, "tray_uuid": "AA"}]}]
        assert farm_staging.compute_tray_signature(a) == farm_staging.compute_tray_signature(b)

    def test_stable_on_empty_payloads(self):
        assert farm_staging.compute_tray_signature([]) == farm_staging.compute_tray_signature([])


class TestAmsChangeHook:
    @pytest.fixture
    def sessions(self, test_engine, monkeypatch):
        """Point farm_staging's own-session opener at the test engine."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        import backend.app.core.database as core_db

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(core_db, "async_session", maker)
        return maker

    async def _seed_farm_staged(self, db, printer_id=1):
        batch = PrintBatch(name="run", quantity=1, status="active", sku_file_id=123)
        db.add(batch)
        await db.flush()
        return await _add_staged(db, printer_id=printer_id, batch_id=batch.id)

    async def test_first_push_seeds_without_release(self, db_session, sessions, monkeypatch):
        await self._seed_farm_staged(db_session)
        _patch_deficit(monkeypatch, AsyncMock(return_value=[]))
        ams = [{"id": 0, "tray": [{"id": 0, "tray_type": "PETG", "remain": 80, "tray_uuid": "AA"}]}]
        assert await farm_staging.maybe_release_on_ams_change(1, ams) == 0  # seed only

    async def test_unchanged_signature_is_noop(self, db_session, sessions, monkeypatch):
        await self._seed_farm_staged(db_session)
        spy = AsyncMock(return_value=[])
        _patch_deficit(monkeypatch, spy)
        ams = [{"id": 0, "tray": [{"id": 0, "tray_type": "PETG", "remain": 80, "tray_uuid": "AA"}]}]
        await farm_staging.maybe_release_on_ams_change(1, ams)
        assert await farm_staging.maybe_release_on_ams_change(1, ams) == 0
        spy.assert_not_awaited()

    async def test_changed_signature_releases_staged_farm_item(self, db_session, sessions, monkeypatch):
        item = await self._seed_farm_staged(db_session)
        _patch_deficit(monkeypatch, AsyncMock(return_value=[]))
        before = [{"id": 0, "tray": [{"id": 0, "tray_type": "PETG", "remain": 5, "tray_uuid": "AA"}]}]
        after = [{"id": 0, "tray": [{"id": 0, "tray_type": "PETG", "remain": 100, "tray_uuid": "BB"}]}]
        await farm_staging.maybe_release_on_ams_change(1, before)  # seed
        assert await farm_staging.maybe_release_on_ams_change(1, after) == 1
        await db_session.refresh(item)
        assert item.manual_start is False
        assert item.filament_short is False

    async def test_changed_signature_releases_unpinned_farm_item(self, db_session, sessions, monkeypatch):
        """An UNPINNED all-short farm item (printer_id NULL) is released on the
        AMS-change path — a spool swap on any printer re-opens the candidate loop."""
        batch = PrintBatch(name="run", quantity=1, status="active", sku_file_id=123)
        db_session.add(batch)
        await db_session.flush()
        item = await _add_staged(db_session, printer_id=None, batch_id=batch.id)
        _patch_deficit(monkeypatch, AsyncMock(return_value=[]))
        before = [{"id": 0, "tray": [{"id": 0, "tray_type": "PETG", "remain": 5, "tray_uuid": "AA"}]}]
        after = [{"id": 0, "tray": [{"id": 0, "tray_type": "PETG", "remain": 100, "tray_uuid": "BB"}]}]
        await farm_staging.maybe_release_on_ams_change(1, before)  # seed
        assert await farm_staging.maybe_release_on_ams_change(1, after) == 1
        await db_session.refresh(item)
        assert item.manual_start is False
        assert item.filament_short is False

    async def test_changed_signature_without_staged_farm_items_short_circuits(self, db_session, sessions, monkeypatch):
        # Staged NON-farm item (batch without sku_file_id): the cheap pre-check
        # skips the release pass entirely on the AMS path.
        batch = PrintBatch(name="plain", quantity=1, status="active")
        db_session.add(batch)
        await db_session.flush()
        await _add_staged(db_session, printer_id=1, batch_id=batch.id)
        spy = AsyncMock(return_value=[])
        _patch_deficit(monkeypatch, spy)
        before = [{"id": 0, "tray": [{"id": 0, "tray_type": "PETG", "remain": 5, "tray_uuid": "AA"}]}]
        after = [{"id": 0, "tray": [{"id": 0, "tray_type": "PETG", "remain": 100, "tray_uuid": "BB"}]}]
        await farm_staging.maybe_release_on_ams_change(1, before)
        assert await farm_staging.maybe_release_on_ams_change(1, after) == 0
        spy.assert_not_awaited()

    async def test_never_raises(self, sessions, monkeypatch):
        # Even a hard failure inside the pass must not escape to the AMS chain.
        def boom(_):
            raise RuntimeError("bad payload")

        monkeypatch.setattr(farm_staging, "compute_tray_signature", boom)
        assert await farm_staging.maybe_release_on_ams_change(1, [{"id": 0}]) == 0
