"""The dispatch-decision contract (2026-08-12; 003-H2S external-spool incident).

``PrintQueueItem.ams_mapping`` is an operator INSTRUCTION — an explicit slot pin — and
never a cache of a derivation. Three things follow, and this file pins all three
through the REAL ``check_queue`` against a real in-memory DB:

1. **The matcher decides at dispatch, every time.** The two "stored mapping → skip the
   computation" branches are gone, so no evaluation can inherit a decision made hours
   ago against different hardware (the incident: one dialog derived ``[254]`` against
   printer 1 and stamped it unseen onto nine items fanned out to printers 2-10).
2. **A pin is an input, not a parallel path.** It narrows the candidate set inside the
   one matcher; a pinned tray that is not there holds the item on the honest
   ``pinned_tray_unavailable`` token instead of a "Low filament" staging lie.
3. **The record is written at the point of no return.** The decision reaches
   ``_start_print`` with the dispatch and is stamped onto the item as it flips to
   'printing' — so a unit held at a later gate keeps its pin, and nothing the matcher
   decided can ever be re-read as an instruction on a later tick.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.print_scheduler import PrintScheduler
from backend.app.services.spool_selection import WAITING_REASON_PINNED_UNAVAILABLE

pytestmark = pytest.mark.asyncio

REQUIREMENTS = [{"slot_id": 1, "type": "PLA", "color": "#FFFFFF", "tray_info_idx": "", "used_grams": 10.0}]
# Two identified trays: gtid 0 (white PLA, the auto-match) and gtid 1 (blue PLA).
TRAYS = [
    {"id": 0, "state": 10, "tray_type": "PLA", "tray_color": "FFFFFFFF", "remain": 90},
    {"id": 1, "state": 10, "tray_type": "PLA", "tray_color": "0000FFFF", "remain": 90},
]


def _status(trays, *, vt_tray=None):
    state = MagicMock()
    state.state = "IDLE"
    state.ams_filament_backup = False
    state.ams_extruder_map = {}
    state.sdcard = True
    state.raw_data = {
        "ams": [{"id": 0, "tray": trays}],
        "ams_extruder_map": {},
        "vt_tray": vt_tray or [],
    }
    return state


async def _harness(*, ams_mapping=None, target_model=None, printer_id=1):
    """Real DB + one pending item (pinned to a printer, or model-targeted)."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    import backend.app.models  # noqa: F401
    from backend.app.core.database import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as db:
        db.add(
            PrintQueueItem(
                printer_id=printer_id,
                target_model=target_model,
                ams_mapping=ams_mapping,
                status="pending",
                position=1,
                archive_id=84,
            )
        )
        await db.commit()
    return engine, maker


async def _tick(scheduler, maker, trays, *, start_mock, mock_pm, vt_tray=None):
    """One full check_queue pass with the live AMS payload scripted."""
    mock_pm.get_status.return_value = _status(trays, vt_tray=vt_tray)
    with (
        patch("backend.app.services.print_scheduler.async_session", maker),
        patch("backend.app.services.print_scheduler.printer_manager", mock_pm),
        patch("backend.app.services.printer_manager.printer_manager", mock_pm),
        patch.object(scheduler, "_check_auto_drying", AsyncMock()),
        patch.object(scheduler, "_get_filament_requirements", AsyncMock(return_value=REQUIREMENTS)),
        patch("backend.app.services.print_scheduler.stagger_policy.budget", AsyncMock(return_value=99)),
        patch.object(scheduler, "_start_print_by_id", start_mock),
    ):
        await scheduler.check_queue()


async def _item(maker):
    async with maker() as db:
        return (await db.execute(select(PrintQueueItem))).scalars().one()


def _printer_manager():
    pm = MagicMock()
    pm.is_connected.return_value = True
    pm.is_quarantined.return_value = False
    pm.is_model_mismatch.return_value = False
    pm.is_awaiting_plate_clear.return_value = False
    pm.request_evidence_pushall.return_value = True
    return pm


class TestTheMatcherDecidesEveryDispatch:
    async def test_a_stored_mapping_no_longer_skips_the_computation(self):
        """THE incident mechanism. The item carries ``[254]`` — an external-spool
        derivation made against another printer — and this printer's external holder is
        empty. The old code dispatched it verbatim (no computation, no gates). Now the
        matcher runs and the stale value is read as a pin it cannot satisfy."""
        engine, maker = await _harness(ams_mapping=json.dumps([254]))
        scheduler, start_mock, pm = PrintScheduler(), AsyncMock(), _printer_manager()
        try:
            await _tick(scheduler, maker, TRAYS, start_mock=start_mock, mock_pm=pm)
            item = await _item(maker)

            start_mock.assert_not_awaited(), "a mapping naming an absent source must never dispatch"
            assert item.waiting_reason == WAITING_REASON_PINNED_UNAVAILABLE
            assert item.status == "pending"
        finally:
            await engine.dispose()

    async def test_the_decision_is_recomputed_from_live_state_not_replayed(self):
        """With no pin, the matcher picks from what is loaded NOW — every tick."""
        engine, maker = await _harness()
        scheduler, start_mock, pm = PrintScheduler(), AsyncMock(), _printer_manager()
        try:
            await _tick(scheduler, maker, TRAYS, start_mock=start_mock, mock_pm=pm)
            start_mock.assert_awaited_once()
            assert start_mock.await_args.args[3] == [0], "the white PLA tray is the live auto-match"
        finally:
            await engine.dispose()

    async def test_an_honoured_pin_overrides_the_auto_match(self):
        """The operator pinned the blue roll; auto-matching would have taken the white
        one. The instruction wins — and it is the DECISION that dispatches."""
        engine, maker = await _harness(ams_mapping=json.dumps([1]))
        scheduler, start_mock, pm = PrintScheduler(), AsyncMock(), _printer_manager()
        try:
            await _tick(scheduler, maker, TRAYS, start_mock=start_mock, mock_pm=pm)
            start_mock.assert_awaited_once()
            assert start_mock.await_args.args[3] == [1]
        finally:
            await engine.dispose()

    async def test_a_pin_to_a_configured_external_tray_dispatches(self):
        """Printer 1's shape: the external holder declares a filament, so it is an
        ordinary candidate and the pin resolves. AMS-vs-external is decided from live
        state — the same call, no branch."""
        engine, maker = await _harness(ams_mapping=json.dumps([254]))
        scheduler, start_mock, pm = PrintScheduler(), AsyncMock(), _printer_manager()
        vt = [{"id": 254, "tray_type": "PLA", "tray_color": "FFFFFFFF", "remain": 80}]
        try:
            await _tick(scheduler, maker, TRAYS, start_mock=start_mock, mock_pm=pm, vt_tray=vt)
            start_mock.assert_awaited_once()
            assert start_mock.await_args.args[3] == [254]
        finally:
            await engine.dispose()


class TestPinnedTrayUnavailableHold:
    async def test_holds_pending_and_unpromoted_with_the_honest_token(self):
        """A missing NAMED roll is not a shortage: no manual_start promotion (which
        needs a human press per row), no "Low filament" banner, no notification lie."""
        engine, maker = await _harness(ams_mapping=json.dumps([9]))
        scheduler, start_mock, pm = PrintScheduler(), AsyncMock(), _printer_manager()
        try:
            await _tick(scheduler, maker, TRAYS, start_mock=start_mock, mock_pm=pm)
            item = await _item(maker)

            assert item.waiting_reason == WAITING_REASON_PINNED_UNAVAILABLE
            assert item.manual_start is False
            assert item.filament_short is False
            assert item.status == "pending"
            start_mock.assert_not_awaited()
        finally:
            await engine.dispose()

    async def test_the_hold_is_self_clearing_when_the_tray_appears(self):
        """LIVENESS: the hold must be a passing state, not a new park. Loading the
        pinned tray dispatches the job with no further human step."""
        engine, maker = await _harness(ams_mapping=json.dumps([2]))
        scheduler, start_mock, pm = PrintScheduler(), AsyncMock(), _printer_manager()
        try:
            await _tick(scheduler, maker, TRAYS, start_mock=start_mock, mock_pm=pm)
            assert (await _item(maker)).waiting_reason == WAITING_REASON_PINNED_UNAVAILABLE
            start_mock.assert_not_awaited()

            # The operator loads the pinned tray (gtid 2).
            with_tray = [*TRAYS, {"id": 2, "state": 10, "tray_type": "PLA", "tray_color": "00FF00FF", "remain": 95}]
            await _tick(scheduler, maker, with_tray, start_mock=start_mock, mock_pm=pm)

            start_mock.assert_awaited_once()
            assert start_mock.await_args.args[3] == [2]
        finally:
            await engine.dispose()

    async def test_the_pin_survives_the_hold(self):
        """The instruction is not consumed by a refusal — it is still there next tick."""
        engine, maker = await _harness(ams_mapping=json.dumps([9]))
        scheduler, start_mock, pm = PrintScheduler(), AsyncMock(), _printer_manager()
        try:
            await _tick(scheduler, maker, TRAYS, start_mock=start_mock, mock_pm=pm)
            assert (await _item(maker)).ams_mapping == "[9]"
        finally:
            await engine.dispose()


class TestNoPartialMappingEverDispatches:
    async def test_an_uncovered_requirement_stages_instead_of_dispatching(self):
        """The total-outcome contract. A mapping with a hole means "nothing feeds this
        extruder" on the wire — it is staged on the ordinary filament lane instead."""
        engine, maker = await _harness()
        scheduler, start_mock, pm = PrintScheduler(), AsyncMock(), _printer_manager()
        wrong = [{"id": 0, "state": 10, "tray_type": "ABS", "tray_color": "000000FF", "remain": 90}]
        try:
            await _tick(scheduler, maker, wrong, start_mock=start_mock, mock_pm=pm)
            item = await _item(maker)

            start_mock.assert_not_awaited()
            assert item.manual_start is True
            assert item.filament_short is True
            assert item.waiting_reason.startswith("Low filament")
            assert item.ams_mapping is None
        finally:
            await engine.dispose()


class TestTheRecordIsWrittenByTheDispatch:
    async def test_the_decision_lands_on_the_item_when_the_print_starts(self):
        """``_start_print`` stamps what actually ran as the item flips to 'printing' —
        the durable record usage tracking, the spent ledger and recovery read back."""
        engine, maker = await _harness(ams_mapping=json.dumps([1]))
        scheduler, pm = PrintScheduler(), _printer_manager()

        async def _fake_start(item_id, printer_id, sem, ams_mapping=None):
            async with maker() as db:
                item = await db.get(PrintQueueItem, item_id)
                item.status = "printing"
                item.ams_mapping = json.dumps(ams_mapping) if ams_mapping else None
                await db.commit()

        try:
            await _tick(scheduler, maker, TRAYS, start_mock=AsyncMock(side_effect=_fake_start), mock_pm=pm)
            item = await _item(maker)
            assert item.status == "printing"
            assert item.ams_mapping == "[1]", "the record of what ran replaces the pin at dispatch"
        finally:
            await engine.dispose()

    async def test_a_held_dispatch_leaves_the_pin_untouched(self):
        """The write is past the USB/capability gates ON PURPOSE: a unit held there must
        keep its instruction, and no decision may be left behind to be re-read as one."""
        engine, maker = await _harness(ams_mapping=json.dumps([1]))
        scheduler, pm = PrintScheduler(), _printer_manager()

        async def _held(item_id, printer_id, sem, ams_mapping=None):
            return  # the hold paths return without touching the item's mapping

        try:
            await _tick(scheduler, maker, TRAYS, start_mock=AsyncMock(side_effect=_held), mock_pm=pm)
            item = await _item(maker)
            assert item.status == "pending"
            assert item.ams_mapping == "[1]"
        finally:
            await engine.dispose()
