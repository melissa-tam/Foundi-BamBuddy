"""The session start (``services/session_start``): a printer's session decisions run from ITS first
fresh report — never from the lifespan, before the printer has said anything.

001-H2S, 2026-10-11: ``rearm_incidents_on_startup`` ran in ``main.lifespan`` before
``init_printer_connections``, read every printer as "unreported", closed nothing — and nothing
re-evaluated incident 574 once 001 did report, so the card said "Toolhead empty" all night over a
toolhead the wire reported fed. The end-to-end case here drives that restart through the real
registry, the real client and the real hub: the open hold is decided on the printer's first fresh
report, and not one push before it. The ownership pins are
``test_code_quality.TestSessionStartOwnership``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.models.printer_incident import (
    KIND_TOOLHEAD_REFILL,
    RESOLVE_REARM,
    STATUS_ESCALATED,
    PrinterIncident,
)
from backend.app.services import printer_event_wiring, printer_incidents
from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.printer_events import FirstFreshReport
from backend.app.services.session_start import SessionStart

pytestmark = pytest.mark.asyncio

_STEPS = (
    "backend.app.services.spool_recovery.reevaluate_session",
    "backend.app.services.pause_recovery.rearm_decision_deadlines",
    "backend.app.services.eject.monitor.reconcile_pending_eject",
    "backend.app.services.printer_action.reread_session",
)


class _Recorder:
    """Every session step, recorded in the order it ran (and optionally held or failing)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.gate: asyncio.Event | None = None

    def step(self, name: str, *, fail: bool = False):
        async def _run(printer_id: int, *_args) -> int:
            self.calls.append((name, printer_id))
            if name == "reconcile" and self.gate is not None:
                await self.gate.wait()
            if fail:
                raise RuntimeError(f"{name} on fire")
            return 0

        return _run


async def _session(start: SessionStart, printer_id: int) -> None:
    await start._sessions[printer_id]  # noqa: SLF001 — the session's own task, to await it


class TestTheSessionStartRunsEveryLaneInOrder:
    async def test_the_steps_run_once_in_order(self):
        rec = _Recorder()
        start = SessionStart(downtime_reconcile=rec.step("reconcile"))
        lost_z = MagicMock()
        with (
            patch(_STEPS[0], new=rec.step("holds")),
            patch(_STEPS[1], new=rec.step("deadline")),
            patch(_STEPS[2], new=rec.step("eject")),
            patch(_STEPS[3], new=rec.step("actions")),
            patch("backend.app.services.pause_recovery.note_session_start", new=lost_z),
        ):
            start.on_first_fresh_report(FirstFreshReport(printer_id=9, connection_epoch=3))
            await _session(start, 9)

        # The downtime reconcile first: a terminal it synthesises answers the record's stop or start
        # before the record's session re-read could void it.
        assert rec.calls == [("reconcile", 9), ("actions", 9), ("holds", 9), ("deadline", 9), ("eject", 9)]
        lost_z.assert_called_once_with(9, 3)
        assert start._sessions == {}  # noqa: SLF001 — the finished session let go of its slot

    async def test_one_lane_failing_never_costs_the_others_their_turn(self, caplog):
        rec = _Recorder()
        start = SessionStart(downtime_reconcile=rec.step("reconcile", fail=True))
        with (
            patch(_STEPS[0], new=rec.step("holds", fail=True)),
            patch(_STEPS[1], new=rec.step("deadline")),
            patch(_STEPS[2], new=rec.step("eject")),
            patch(_STEPS[3], new=rec.step("actions")),
            patch("backend.app.services.pause_recovery.note_session_start"),
        ):
            start.on_first_fresh_report(FirstFreshReport(printer_id=9, connection_epoch=1))
            await _session(start, 9)

        assert [name for name, _ in rec.calls] == ["reconcile", "actions", "holds", "deadline", "eject"]
        assert "downtime reconcile failed" in caplog.text and "open holds failed" in caplog.text

    async def test_a_newer_session_waits_for_the_older_ones_steps(self):
        """A quick reconnect never lets two sessions decide one printer at once."""
        rec = _Recorder()
        rec.gate = asyncio.Event()
        start = SessionStart(downtime_reconcile=rec.step("reconcile"))
        with (
            patch(_STEPS[0], new=rec.step("holds")),
            patch(_STEPS[1], new=rec.step("deadline")),
            patch(_STEPS[2], new=rec.step("eject")),
            patch(_STEPS[3], new=rec.step("actions")),
            patch("backend.app.services.pause_recovery.note_session_start"),
        ):
            start.on_first_fresh_report(FirstFreshReport(printer_id=9, connection_epoch=1))
            first = start._sessions[9]  # noqa: SLF001
            await asyncio.sleep(0)
            start.on_first_fresh_report(FirstFreshReport(printer_id=9, connection_epoch=2))
            for _ in range(5):
                await asyncio.sleep(0)
            assert rec.calls == [("reconcile", 9)]  # the second session has not started

            rec.gate.set()
            await first
            await _session(start, 9)

        assert [name for name, _ in rec.calls] == ["reconcile", "actions", "holds", "deadline", "eject"] * 2


@pytest.fixture
def _own_sessions(own_session_factory, monkeypatch):
    """The lanes open their own sessions (module convention): point them at the test engine."""
    import backend.app.core.database as core_db

    monkeypatch.setattr(core_db, "async_session", own_session_factory)


@pytest.fixture
def _wired():
    """The lifespan's wiring (no lifespan runs under a unit test); the downtime reconcile is
    ``main``'s and has no archive to judge here."""
    printer_event_wiring.wire(downtime_reconcile=AsyncMock(return_value=0))
    yield
    printer_event_wiring.unwire()


async def _still_open(db, printer_id: int) -> bool:
    db.expunge_all()
    return await printer_incidents.get_open(db, printer_id, kinds={KIND_TOOLHEAD_REFILL}) is not None


async def _settle() -> None:
    """Let the registry's hops onto the loop and anything they spawn run."""
    for _ in range(20):
        await asyncio.sleep(0.01)


@pytest.mark.usefixtures("_own_sessions", "_wired")
class TestARestartDecidesOnTheFirstFreshReport:
    async def test_the_open_toolhead_hold_is_re_evaluated_on_that_report_and_not_before(
        self, db_session, printer_factory, monkeypatch
    ):
        from backend.app.services.printer_manager import printer_manager

        # What a restart leaves: the hold's row, and the projection the lifespan rehydrated.
        printer = await printer_factory()
        row = await printer_incidents.open_new(
            db_session,
            printer_id=printer.id,
            job_id="task-1",
            item_id=None,
            kind=KIND_TOOLHEAD_REFILL,
            code="",
            codes="",
            slot_global_tray=0,
            status=STATUS_ESCALATED,
        )
        await printer_incidents.rehydrate(db_session)

        # The registry builds the printer's client (no network) on the running loop.
        monkeypatch.setattr(BambuMQTTClient, "connect", lambda self, loop=None: None)
        printer_manager.set_event_loop(asyncio.get_running_loop())
        await printer_manager.connect_printer(printer)
        client = printer_manager.get_client(printer.id)
        client._client = MagicMock()
        client._client.subscribe.return_value = (0, 1)

        await _settle()
        assert await _still_open(db_session, printer.id), "decided before the printer reported"

        client._on_connect(client._client, None, None, 0)  # the session opens: cached state, no report
        await _settle()
        assert await _still_open(db_session, printer.id), "decided on the connect broadcast"

        client._process_message({"print": {"gcode_state": "IDLE", "gcode_file": "", "subtask_id": "task-1"}})
        for _ in range(100):
            if not await _still_open(db_session, printer.id):
                break
            await asyncio.sleep(0.02)

        assert not await _still_open(db_session, printer.id), "the first fresh report decided nothing"
        closed = await db_session.get(PrinterIncident, row.id)
        assert closed.resolve_source == RESOLVE_REARM  # the toolhead cell's: the job it held is over
