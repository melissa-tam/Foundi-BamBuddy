"""Maintenance mode's scheduled RETURN TO SERVICE (``printer_incident.release_at``, 2026-10-16).

Drives the public verbs — ``service_hold.enter`` / ``reschedule`` / ``exit`` / ``release_due`` and
the scheduler's ``check_queue`` — against the real incident store on the test engine. Only the
printer manager's broadcast is replaced (recorded), because a hold must not need a socket.

The shapes held here:

* a pass BEFORE the time leaves the printer held; the first pass AFTER it exits the hold once,
  through the hold's one closer (``source=scheduled``), kicks the scheduler and broadcasts;
* a reschedule moves the release and a clear removes it;
* the time survives a restart: a fresh projection built from the rows releases on the first pass;
* an operator's Exit first leaves nothing for the schedule to close (exactly one exit);
* a time that is not in the future is refused and changes nothing;
* entering with a time on a printer already held reschedules that row;
* the per-pass check costs no query (it reads the projection).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import event

from backend.app.models.printer_incident import (
    KIND_SERVICE_HOLD,
    RESOLVE_OPERATOR,
    RESOLVE_SCHEDULED,
    STATUS_RESOLVED,
    PrinterIncident,
)
from backend.app.services import printer_incidents, service_hold
from backend.app.services.dispatch_kick import dispatch_kick

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 10, 16, 9, 0, 0)
_AT = _NOW + timedelta(hours=2)


@pytest.fixture(autouse=True)
def _reset():
    printer_incidents._reset_state()
    dispatch_kick._reasons.clear()
    yield
    printer_incidents._reset_state()


class _Broadcasts:
    """``printer_manager`` reduced to the one call the verbs make: the status emit."""

    def __init__(self) -> None:
        self.printers: list[int] = []

    async def _broadcast_status_change(self, printer_id: int) -> None:
        self.printers.append(printer_id)


@pytest.fixture
def broadcasts(monkeypatch) -> _Broadcasts:
    spy = _Broadcasts()
    monkeypatch.setattr(service_hold, "printer_manager", spy)
    return spy


async def _closed_row(db, incident_id: int) -> PrinterIncident:
    row = await db.get(PrinterIncident, incident_id, populate_existing=True)
    assert row is not None
    return row


async def _enter(db, printer_id: int, release_at: datetime | None) -> service_hold.HoldVerdict:
    verdict = await service_hold.enter(db, printer_id, actor="raymond", release_at=release_at, now=_NOW)
    assert isinstance(verdict, service_hold.HoldVerdict), verdict
    return verdict


async def _hold_id(db, printer_id: int) -> int:
    row = await printer_incidents.get_open(db, printer_id, kinds={KIND_SERVICE_HOLD})
    assert row is not None
    return row.id


class TestEnterWithATime:
    async def test_a_pass_before_the_time_leaves_it_held_and_one_after_exits_once(
        self, db_session, printer_factory, broadcasts
    ):
        printer = await printer_factory()
        verdict = await _enter(db_session, printer.id, _AT)
        assert verdict.release_at == _AT
        hold_id = await _hold_id(db_session, printer.id)
        broadcasts.printers.clear()

        assert await service_hold.release_due(db_session, now=_AT - timedelta(seconds=1)) == 0
        assert printer_incidents.automation_held(printer.id) is True
        assert broadcasts.printers == []

        assert await service_hold.release_due(db_session, now=_AT) == 1
        assert printer_incidents.automation_held(printer.id) is False
        row = await _closed_row(db_session, hold_id)
        assert (row.status, row.resolve_source) == (STATUS_RESOLVED, RESOLVE_SCHEDULED)
        assert dispatch_kick._reasons[-1][1:] == ("service_hold_released", printer.id)
        assert broadcasts.printers == [printer.id]

        # Level-triggered and closed: the next pass has nothing left to release.
        assert await service_hold.release_due(db_session, now=_AT + timedelta(hours=1)) == 0
        assert broadcasts.printers == [printer.id]

    async def test_the_time_is_projected_on_the_status_payload(self, db_session, printer_factory, broadcasts):
        from backend.app.services.printer_manager import service_hold_payload

        printer = await printer_factory()
        await _enter(db_session, printer.id, _AT)

        payload = service_hold_payload(printer.id)

        assert payload is not None
        assert payload["release_at"] == _AT.isoformat()

    async def test_an_aware_time_is_stored_as_naive_utc(self, db_session, printer_factory, broadcasts):
        printer = await printer_factory()
        aware = (_AT).replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=-7)))

        verdict = await _enter(db_session, printer.id, aware)

        assert verdict.release_at == _AT
        row = await printer_incidents.get_open(db_session, printer.id, kinds={KIND_SERVICE_HOLD})
        assert row is not None and row.release_at == _AT

    async def test_entering_with_no_time_never_releases(self, db_session, printer_factory, broadcasts):
        printer = await printer_factory()
        verdict = await _enter(db_session, printer.id, None)

        assert verdict.release_at is None
        assert await service_hold.release_due(db_session, now=_NOW + timedelta(days=365)) == 0
        assert printer_incidents.automation_held(printer.id) is True


class TestReschedule:
    async def test_a_later_time_moves_the_release(self, db_session, printer_factory, broadcasts):
        printer = await printer_factory()
        await _enter(db_session, printer.id, _AT)
        later = _AT + timedelta(hours=3)

        verdict = await service_hold.reschedule(db_session, printer.id, later, actor="raymond", now=_NOW)

        assert verdict == service_hold.RescheduleVerdict(held=True, release_at=later)
        assert await service_hold.release_due(db_session, now=_AT) == 0
        assert printer_incidents.automation_held(printer.id) is True
        assert await service_hold.release_due(db_session, now=later) == 1
        assert printer_incidents.automation_held(printer.id) is False

    async def test_a_clear_removes_the_release(self, db_session, printer_factory, broadcasts):
        printer = await printer_factory()
        await _enter(db_session, printer.id, _AT)

        verdict = await service_hold.reschedule(db_session, printer.id, None, actor="raymond", now=_NOW)

        assert verdict == service_hold.RescheduleVerdict(held=True, release_at=None)
        assert await service_hold.release_due(db_session, now=_AT + timedelta(days=30)) == 0
        assert printer_incidents.automation_held(printer.id) is True
        row = await printer_incidents.get_open(db_session, printer.id, kinds={KIND_SERVICE_HOLD})
        assert row is not None and row.release_at is None

    async def test_a_reschedule_broadcasts_the_new_time(self, db_session, printer_factory, broadcasts):
        printer = await printer_factory()
        await _enter(db_session, printer.id, None)
        broadcasts.printers.clear()

        await service_hold.reschedule(db_session, printer.id, _AT, actor="raymond", now=_NOW)

        assert broadcasts.printers == [printer.id]

    async def test_an_unheld_printer_has_no_time_to_set(self, db_session, printer_factory, broadcasts):
        printer = await printer_factory()

        verdict = await service_hold.reschedule(db_session, printer.id, _AT, actor="raymond", now=_NOW)

        assert verdict == service_hold.RescheduleVerdict(held=False)
        assert printer_incidents.due_releases(_AT + timedelta(days=1)) == ()

    async def test_re_entering_with_a_time_reschedules_the_standing_row(self, db_session, printer_factory, broadcasts):
        printer = await printer_factory()
        await _enter(db_session, printer.id, None)
        hold_id = await _hold_id(db_session, printer.id)

        verdict = await _enter(db_session, printer.id, _AT)

        assert verdict.already_held is True
        assert verdict.release_at == _AT
        assert [row.id for row in await printer_incidents.open_rows(db_session, printer.id)] == [hold_id]
        assert await service_hold.release_due(db_session, now=_AT) == 1

    async def test_re_entering_with_no_time_keeps_the_schedule(self, db_session, printer_factory, broadcasts):
        printer = await printer_factory()
        await _enter(db_session, printer.id, _AT)

        verdict = await _enter(db_session, printer.id, None)

        assert verdict.already_held is True
        assert verdict.release_at == _AT

    async def test_a_time_changed_after_the_pass_listed_the_row_is_honoured(
        self, db_session, printer_factory, broadcasts
    ):
        """The exit's own re-check: the pass read the row due, the operator moved the time before
        the close ran — the hold stays."""
        printer = await printer_factory()
        await _enter(db_session, printer.id, _AT)
        assert len(printer_incidents.due_releases(_AT)) == 1
        await service_hold.reschedule(db_session, printer.id, _AT + timedelta(hours=1), actor="raymond", now=_NOW)

        released = await service_hold.exit(
            db_session, printer.id, actor=service_hold.ACTOR_SCHEDULE, source=RESOLVE_SCHEDULED, due_by=_AT
        )

        assert released is False
        assert printer_incidents.automation_held(printer.id) is True


class TestPastTimeRefused:
    async def test_enter_with_a_past_time_opens_nothing(self, db_session, printer_factory, broadcasts):
        printer = await printer_factory()

        verdict = await service_hold.enter(
            db_session, printer.id, actor="raymond", release_at=_NOW - timedelta(minutes=1), now=_NOW
        )

        assert verdict == service_hold.ReleaseRefused(reason=service_hold.RELEASE_IN_PAST)
        assert printer_incidents.automation_held(printer.id) is False
        assert await printer_incidents.open_rows(db_session, printer.id) == []

    async def test_now_itself_is_not_the_future(self, db_session, printer_factory, broadcasts):
        printer = await printer_factory()
        await _enter(db_session, printer.id, _AT)

        verdict = await service_hold.reschedule(db_session, printer.id, _NOW, actor="raymond", now=_NOW)

        assert verdict == service_hold.ReleaseRefused(reason=service_hold.RELEASE_IN_PAST)
        row = await printer_incidents.get_open(db_session, printer.id, kinds={KIND_SERVICE_HOLD})
        assert row is not None and row.release_at == _AT  # unchanged


class TestExactlyOnce:
    async def test_an_operator_exit_first_leaves_nothing_for_the_schedule(
        self, db_session, printer_factory, broadcasts
    ):
        printer = await printer_factory()
        await _enter(db_session, printer.id, _AT)
        hold_id = await _hold_id(db_session, printer.id)

        assert await service_hold.exit(db_session, printer.id, actor="raymond") is True
        kicks = len(dispatch_kick._reasons)

        assert await service_hold.release_due(db_session, now=_AT + timedelta(hours=1)) == 0
        row = await _closed_row(db_session, hold_id)
        assert row.resolve_source == RESOLVE_OPERATOR
        assert len(dispatch_kick._reasons) == kicks  # no second exit, no second kick

    async def test_a_new_hold_after_the_release_is_its_own(self, db_session, printer_factory, broadcasts):
        """A hold re-entered with no time after a scheduled release is not released by the old time."""
        printer = await printer_factory()
        await _enter(db_session, printer.id, _AT)
        assert await service_hold.release_due(db_session, now=_AT) == 1

        await service_hold.enter(db_session, printer.id, actor="raymond", now=_AT)

        assert await service_hold.release_due(db_session, now=_AT + timedelta(hours=1)) == 0
        assert printer_incidents.automation_held(printer.id) is True


class TestRestart:
    async def test_a_persisted_time_releases_on_the_first_pass_after_a_restart(
        self, db_session, printer_factory, broadcasts, own_session_factory
    ):
        printer = await printer_factory()
        await _enter(db_session, printer.id, _AT)
        hold_id = await _hold_id(db_session, printer.id)

        # The restart: process state gone, the projection rebuilt from the rows in a new session.
        printer_incidents._reset_state()
        assert printer_incidents.due_releases(_AT) == ()
        async with own_session_factory() as boot:
            assert await printer_incidents.rehydrate(boot) == 1

        # The time passed while the process was down: the first pass releases it.
        async with own_session_factory() as tick:
            assert await service_hold.release_due(tick, now=_AT + timedelta(minutes=30)) == 1
        async with own_session_factory() as check:
            row = await _closed_row(check, hold_id)
        assert (row.status, row.resolve_source) == (STATUS_RESOLVED, RESOLVE_SCHEDULED)
        assert printer_incidents.automation_held(printer.id) is False


class TestPassCost:
    async def test_a_pass_with_nothing_due_runs_no_query(self, db_session, printer_factory, broadcasts, test_engine):
        printer = await printer_factory()
        await _enter(db_session, printer.id, _AT)
        statements: list[str] = []

        def _count(conn, cursor, statement, *args):
            statements.append(statement)

        event.listen(test_engine.sync_engine, "before_cursor_execute", _count)
        try:
            assert await service_hold.release_due(db_session, now=_AT - timedelta(minutes=1)) == 0
        finally:
            event.remove(test_engine.sync_engine, "before_cursor_execute", _count)

        assert statements == []


class TestTheSchedulerPass:
    async def test_check_queue_releases_a_due_hold_before_the_pending_query(
        self, db_session, printer_factory, broadcasts
    ):
        """REGISTRATION + ORDER: ``check_queue`` runs the release on the tick's session, BEFORE it
        reads the pending units, so a released printer dispatches in the same pass."""
        from backend.app.services.print_scheduler import PrintScheduler

        order: list[str] = []
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []

        async def _execute(statement, *args, **kwargs):
            # Every sibling watch is stubbed, so the one print_queue read left is the pending query.
            if "FROM print_queue" in str(statement):
                order.append("pending_query")
            return empty

        async def _release(db, **kwargs):
            order.append("release_due")
            return 0

        with (
            patch("backend.app.services.print_scheduler.async_session") as mock_session,
            patch("backend.app.services.farm_stall.check_stalled_prints", new=AsyncMock()),
            patch("backend.app.services.farm_stall.check_paused_prints", new=AsyncMock()),
            patch("backend.app.services.farm_stall.check_attention_reminders", new=AsyncMock()),
            patch("backend.app.services.farm_stall.check_foreign_paused_printers", new=AsyncMock()),
            patch("backend.app.services.farm_stall.check_unowned_ejects", new=AsyncMock()),
            patch("backend.app.services.farm_stall.check_ams_wedged_idle", new=AsyncMock()),
            patch("backend.app.services.farm_stall.check_waiting_on_printers", new=AsyncMock()),
            patch("backend.app.services.spool_recovery.sweep_open_incidents", new=AsyncMock()),
            patch("backend.app.services.spool_tagless.reconcile_slot_config", new=AsyncMock()),
            patch("backend.app.services.print_scheduler.maybe_release_periodic", new=AsyncMock()),
            patch.object(service_hold, "release_due", new=_release),
        ):
            mock_db = AsyncMock()
            mock_db.execute = AsyncMock(side_effect=_execute)
            mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session.return_value.__aexit__ = AsyncMock(return_value=False)
            await PrintScheduler().check_queue()

        assert "release_due" in order and "pending_query" in order, order
        assert order.index("release_due") < order.index("pending_query"), order

    async def test_a_failing_release_does_not_kill_the_tick(self):
        from backend.app.services.print_scheduler import PrintScheduler

        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []
        with (
            patch("backend.app.services.print_scheduler.async_session") as mock_session,
            patch("backend.app.services.farm_stall.check_stalled_prints", new=AsyncMock()),
            patch("backend.app.services.farm_stall.check_paused_prints", new=AsyncMock()),
            patch("backend.app.services.farm_stall.check_attention_reminders", new=AsyncMock()),
            patch.object(service_hold, "release_due", new=AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            mock_db = AsyncMock()
            mock_db.execute = AsyncMock(return_value=empty)
            mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session.return_value.__aexit__ = AsyncMock(return_value=False)
            await PrintScheduler().check_queue()  # must not raise
