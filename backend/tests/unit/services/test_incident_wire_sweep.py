"""The wire-clear incident sweep — an AMS hold ends when its FAULT ends.

Before 2026-08-29 an incident could close on a resume, on a job terminal, on the
refill auto-resume, or on a restart's rearm. Nothing closed one because the fault
had CLEARED, so an operator who fixed a printer that was not going to print again
by itself left the hold — and the dispatch block that IS the hold — standing.
001-H2S incident #60: escalated 01:25 on ``0700_0006``, printer physically clean
and reading IDLE by 15:30, still held at 16:00, and because exclusivity is
one-open-incident-per-printer it was also blocking every FUTURE incident on that
printer.

The suppression cases are the whole design: this sweep can end a hold nobody
resumed, so each guard is pinned against the reading that would make it wrong.

**The sweep has TWO lanes since 2026-09-11** (003-H2S), because "the wire went
quiet" means opposite things for different kinds. For a ``wire`` kind it is the
fault ending. For an AMS ``physical`` kind it is nothing at all — the firmware wipes
its HMS list at every terminal, so a stuck filament reads exactly like a repaired
one — and that hold ends only on POSITIVE evidence that filament moved through the
path again. So the WIRE-lane cases below are keyed on ``KIND_JAM`` (the wire class
#60's own mechanical sibling carried) and the repair lane has its own class.

**No dwell since 2026-10-11** ("using time as a gate is not the correct way to discern
status complete or incompleteness"). What the 120 s dwell stood in for is EVENTS: the sweep
reads only the printer's LIVE report, only once the printer's session start has re-evaluated
its holds, and it stands while the printer's terminal is being handled — the terminal decides
the holds it answers.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_incident import (
    KIND_JAM,
    KIND_PHYSICAL,
    KIND_RUNOUT,
    RESOLVE_REPAIR_OBSERVED,
    STATUS_ESCALATED,
    STATUS_RECOVERING,
)
from backend.app.services import incident_resolution, printer_action, printer_incidents, spool_recovery
from backend.app.services.bambu_mqtt import HMSError, PrinterState
from backend.tests._fixtures.feed import ScriptedFeed

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset():
    spool_recovery._reset_state()
    yield
    spool_recovery._reset_state()


@pytest.fixture(autouse=True)
def _own_sessions(test_engine, monkeypatch):
    """The sweep opens its own session (module convention) — point it at the test
    engine, exactly as the recovery suite does for the other lifecycle entries."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import backend.app.core.database as core_db

    monkeypatch.setattr(
        core_db, "async_session", async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    )


def _ptfe_breakage_hms(ams_id: int = 0, tray_id: int = 3) -> HMSError:
    """The exact code incident #60 carried: ``0700_0006`` — "AMS A has detected a
    breakage of the PTFE tube during filament loading". PHYSICAL class, so it is
    actionable and a swap can never clear it."""
    attr = 0x07000000 | (ams_id << 16) | ((0x20 + tray_id) << 8)
    return HMSError(code="0x20006", attr=attr, module=7, severity=2, full_code=f"{attr:08X}00020006")


def _feed_fault_hms() -> HMSError:
    """``0700_8006`` — the MECHANICAL sibling #60 arrived paired with."""
    return HMSError(code="8006", attr=0x07008006, module=7, severity=2, full_code="07008006")


def _state(live: str | None, hms: list | None = None, *, fresh: bool = True) -> PrinterState | None:
    """A printer report. ``fresh`` — the current session's report APPLIED (``live_reading.is_fresh``);
    the sweep reads nothing else."""
    if live is None:
        return None
    st = PrinterState()
    st.state = live
    st.subtask_id = "task-1"
    st.hms_errors = hms or []
    st.connected = fresh
    st.report_epoch = st.connection_epoch if fresh else None
    return st


def _wire(monkeypatch, state, printer_id: int | None = None, *, session_started: bool = True) -> None:
    """The printer's live state, its session, and its client — whose toolhead FEED STATE
    (``feed_state``, invariant 16: what ``Context.feed`` reads) is a real tracker stepped off the
    scripted state (``ScriptedFeed``), as the client steps it per push, and whose job layer is the
    state's. ``session_started`` — the printer's session start re-evaluated its holds for this
    session (the sweep acts only after it)."""
    feed = ScriptedFeed()
    client = None
    if state is not None:
        feed.settle(state)
        client = SimpleNamespace(
            feed=lambda: feed.reading,
            job_peaks=lambda: SimpleNamespace(layer_num=int(getattr(state, "layer_num", 0) or 0)),
        )
    monkeypatch.setattr(spool_recovery.printer_manager, "get_status", lambda _pid: state)
    monkeypatch.setattr(spool_recovery.printer_manager, "get_client", lambda _pid: client)
    if printer_id is not None and state is not None and session_started:
        spool_recovery._session_evaluated[printer_id] = state.connection_epoch


async def _held(
    db, printer_id, *, kind=KIND_JAM, status=STATUS_ESCALATED, item_id=None, code="0700_0006", job_id="task-0"
):
    """A held incident of a WIRE-resolved kind by default.

    ``kind`` defaults to ``jam`` rather than ``physical``: the sweep branches on the
    kind's RESOLUTION CLASS, and since 2026-09-11 an AMS physical fault is
    ``repair``-resolved, which is a different lane with different evidence. The
    ``0700_0006`` code stays because it is #60's own, and the guard under test is
    whole-printer fault liveness, not the code's class.
    """
    return await printer_incidents.open_new(
        db,
        printer_id=printer_id,
        job_id=job_id,
        item_id=item_id,
        kind=kind,
        code=code,
        codes=f"physical_fault:{code}",
        slot_global_tray=3,
        status=status,
    )


async def _row(db, printer_id):
    db.expunge_all()
    return await printer_incidents.get_open(db, printer_id)


async def _closed_source(db, printer_id):
    from sqlalchemy import select

    from backend.app.models.printer_incident import PrinterIncident

    db.expunge_all()
    return (
        (await db.execute(select(PrinterIncident).where(PrinterIncident.printer_id == printer_id)))
        .scalar_one()
        .resolve_source
    )


class TestHoldOverPredicate:
    """``incident_resolution._hold_over`` is the ONE reading of "the printer says the
    hold is over", consumed by the wire lane's ``sweep_tick`` and ``startup`` cells.
    The agreement is pinned rather than assumed: two copies of this rule drifting is
    how a restart and a running process come to disagree about whether a printer is
    held."""

    @pytest.mark.parametrize(
        ("live", "expected"),
        [
            (None, False),  # the printer has not reported — absence of evidence
            ("", False),
            ("UNKNOWN", False),
            ("PAUSE", False),  # PAUSE *is* the hold
            ("RUNNING", True),
            ("IDLE", True),
            ("FINISH", True),
            ("FAILED", True),
        ],
    )
    async def test_hold_over_predicate_matches_startup_rearm(
        self, db_session, printer_factory, monkeypatch, live, expected
    ):
        printer = await printer_factory()
        incident = await _held(db_session, printer.id)
        state = _state(live)
        _wire(monkeypatch, state)

        # The predicate...
        verdict, reported = incident_resolution._hold_over(state)
        assert verdict is expected
        assert reported == (live or "")
        # ...the table cell that consumes it...
        assert (
            incident_resolution.resolve(
                incident, "startup", incident_resolution.Context(state=state, driver_live=False)
            ).close
            is expected
        )

        # ...and the rearm that consumes it, on the identical evidence.
        assert (await spool_recovery.reevaluate_session(printer.id) == 1) is expected
        assert (await _row(db_session, printer.id) is None) is expected


class TestWireClearSweep:
    async def test_escalated_incident_on_idle_fault_free_printer_closes(self, db_session, printer_factory, monkeypatch):
        """LIVENESS — the exact #60 shape, and the one case that fails on the
        2026-08-29 build: escalated hold, printer reporting live, IDLE, wire clean. It
        closes on the FIRST sweep — the live report is the evidence, no dwell — the queue
        token goes with it, and the CHIP clears."""
        printer = await printer_factory()
        item = PrintQueueItem(
            printer_id=printer.id,
            status="printing",
            position=1,
            waiting_reason=printer_incidents.WAITING_REASON_PHYSICAL,
        )
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)
        await _held(db_session, printer.id, item_id=item.id)
        _wire(monkeypatch, _state("IDLE", []), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 1

        assert await _closed_source(db_session, printer.id) == "wire_clear"
        # The chip clears — the operator-visible half of the fix.
        assert printer_incidents.snapshot(printer.id) is None
        # ...and so does the queue row's projection of the hold.
        assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None

    async def test_a_standing_sibling_fault_keeps_the_hold_open(self, db_session, printer_factory, monkeypatch):
        """The corrected guard. #60 carried the PAIR ``0700_8006`` + ``0700_0006``,
        so a close keyed on the incident's OWN code leaving the live list would have
        ended the hold with an actionable fault still standing. The guard is
        whole-printer: ``live_candidates`` EMPTY, through the one taxonomy."""
        printer = await printer_factory()
        await _held(db_session, printer.id, code="0700_0006")
        # The incident's own code is gone; its mechanical sibling is not.
        _wire(monkeypatch, _state("IDLE", [_feed_fault_hms()]), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 0
        assert await _row(db_session, printer.id) is not None

    async def test_the_incidents_own_fault_still_standing_keeps_it_open(self, db_session, printer_factory, monkeypatch):
        """The 2026-08-19 shape, held correctly: a fault live on an IDLE printer is a
        REAL hold — idleness is not recovery."""
        printer = await printer_factory()
        await _held(db_session, printer.id)
        _wire(monkeypatch, _state("IDLE", [_ptfe_breakage_hms()]), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 0
        assert await _row(db_session, printer.id) is not None

    async def test_a_paused_printer_is_never_closed(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        await _held(db_session, printer.id, kind=KIND_RUNOUT)
        _wire(monkeypatch, _state("PAUSE", []), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 0
        assert await _row(db_session, printer.id) is not None

    async def test_a_report_off_the_current_session_is_never_evidence(self, db_session, printer_factory, monkeypatch):
        """A reconnect re-broadcasts the PREVIOUS session's cached state, and a disconnected printer's
        cache is a memory: neither is the printer's live report (``live_reading.reads_live``)."""
        printer = await printer_factory()
        await _held(db_session, printer.id)
        _wire(monkeypatch, _state("IDLE", [], fresh=False), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 0
        assert await _row(db_session, printer.id) is not None

    @pytest.mark.parametrize("live", ["", "UNKNOWN", None])
    async def test_an_unreported_state_is_never_closed(self, db_session, printer_factory, monkeypatch, live):
        printer = await printer_factory()
        await _held(db_session, printer.id)
        _wire(monkeypatch, _state(live), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 0
        assert await _row(db_session, printer.id) is not None

    async def test_a_recovering_incident_is_never_closed(self, db_session, printer_factory, monkeypatch):
        """``recovering`` has a live driver (or the restart lane's re-entry); closing
        it from underneath would race the machine that is acting on it."""
        printer = await printer_factory()
        await _held(db_session, printer.id, status=STATUS_RECOVERING)
        _wire(monkeypatch, _state("IDLE", []), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 0
        assert await _row(db_session, printer.id) is not None

    async def test_a_terminal_being_handled_decides_first(self, db_session, printer_factory, monkeypatch):
        """THE ordering the dwell used to guarantee, now an EVENT: the printer reported its job's end
        (FAILED, the firmware's HMS list wiped) and ``main.on_print_complete`` is still handling it — it
        reads this very hold to classify the stop (the 2026-09-11 fault-stop requeue) before closing it.
        The sweep stands until the handler returns, then the reading is the sweep's again."""
        printer = await printer_factory()
        await _held(db_session, printer.id)
        _wire(monkeypatch, _state("FAILED", []), printer.id)

        spool_recovery.printer_manager._terminals_in_flight[printer.id] = 1  # noqa: SLF001
        assert await spool_recovery.sweep_open_incidents() == 0
        assert await _row(db_session, printer.id) is not None

        spool_recovery.printer_manager._terminals_in_flight.pop(printer.id)  # noqa: SLF001
        assert await spool_recovery.sweep_open_incidents() == 1

    async def test_the_session_start_decides_a_new_session_first(self, db_session, printer_factory, monkeypatch):
        """A sweep tick between a printer's first fresh report and its session start's re-evaluation
        (the downtime reconcile runs first there, classifying a synthesised terminal against the holds
        that stood) must never pre-empt it: the sweep acts only once the session's own re-evaluation
        ran — an event, never a window."""
        printer = await printer_factory()
        await _held(db_session, printer.id, kind=KIND_PHYSICAL, code="0700_8004")
        state = _state("IDLE", [])
        _wire(monkeypatch, state, printer.id, session_started=False)

        assert await spool_recovery.sweep_open_incidents() == 0
        assert spool_recovery._session_evaluated == {}

        await spool_recovery.reevaluate_session(printer.id)
        assert spool_recovery._session_evaluated[printer.id] == state.connection_epoch

    async def test_slow_firmware_closes_on_the_report_however_late(self, db_session, printer_factory, monkeypatch):
        """No window decides anything: a printer that takes as long as it takes to report clean is
        swept clean on the report that says so, and a report that does not say so leaves it held."""
        printer = await printer_factory()
        await _held(db_session, printer.id)
        _wire(monkeypatch, _state("PAUSE", [_ptfe_breakage_hms()]), printer.id)
        for _ in range(5):
            assert await spool_recovery.sweep_open_incidents() == 0
        _wire(monkeypatch, _state("IDLE", []), printer.id)
        assert await spool_recovery.sweep_open_incidents() == 1

    async def test_a_resumed_jam_closes_once_the_print_prints_on(self, db_session, printer_factory, monkeypatch):
        """LIVENESS (R27): an escalated jam the operator cleared and resumed — the print RUNNING fed, the wire
        clean — is not held a moment longer than its layer: the sweep stands while the print is still on the
        fault's own layer (a flap or a re-pause never advances one) and closes it on the first layer printed
        past the fault, with no dwell and no click."""
        printer = await printer_factory()
        await printer_incidents.open_new(
            db_session,
            printer_id=printer.id,
            job_id="task-1",
            item_id=None,
            kind=KIND_JAM,
            code="0700_8010",
            codes="mechanical_feed:0700_8010",
            slot_global_tray=1,
            status=STATUS_ESCALATED,
            fault_layer=30,
        )
        state = _state("RUNNING", [])
        state.tray_now = 1
        state.layer_num = 30
        _wire(monkeypatch, state, printer.id)
        assert await spool_recovery.sweep_open_incidents() == 0
        assert await _row(db_session, printer.id) is not None

        state = _state("RUNNING", [])
        state.tray_now = 1
        state.layer_num = 31
        _wire(monkeypatch, state, printer.id)
        assert await spool_recovery.sweep_open_incidents() == 1
        assert await _closed_source(db_session, printer.id) == "wire_clear"

    async def test_one_bad_incident_does_not_abort_the_sweep(self, db_session, printer_factory, monkeypatch):
        """Invariant 10: this runs from the scheduler tick, so nothing in it may
        propagate — and one unhealthy row must not shield the rest of the fleet."""
        bad = await printer_factory()
        good = await printer_factory()
        await _held(db_session, bad.id)
        await _held(db_session, good.id)
        state = _state("IDLE", [])
        _wire(monkeypatch, state, bad.id)
        spool_recovery._session_evaluated[good.id] = state.connection_epoch

        real_close = printer_incidents.close
        bad_incident = await printer_incidents.get_open(db_session, bad.id)

        async def _boom(db, incident_id, **kwargs):
            if incident_id == bad_incident.id:
                raise RuntimeError("boom")
            return await real_close(db, incident_id, **kwargs)

        with patch.object(printer_incidents, "close", new=AsyncMock(side_effect=_boom)):
            closed = await spool_recovery.sweep_open_incidents()

        assert closed == 1  # the healthy printer was still swept
        assert await _row(db_session, good.id) is None
        assert await _row(db_session, bad.id) is not None


class TestRepairClassSweep:
    """An AMS ``physical`` hold ends on POSITIVE evidence that the path is clear.

    003-H2S 2026-09-11: ``0700_0012`` + ``0700_8004``, filament physically stuck in
    the shared PTFE path. The operator stopped the print, the firmware wiped its HMS
    list at the terminal, the incident closed, and two minutes later the scheduler
    dispatched the next unit onto the same printer and the same stuck filament. An
    empty HMS list is not a repair, and on the data it never was: of the 8 physical
    rows ever closed ``wire_clear`` on this farm, ALL eight were external-holder
    PROMPT codes; every AMS-side one closed at a terminal or at a resume.
    """

    async def _physical(self, db, printer_id, *, code="0700_8004", job_id="task-1", fault_layer=5):
        return await printer_incidents.open_new(
            db,
            printer_id=printer_id,
            job_id=job_id,
            item_id=None,
            kind=KIND_PHYSICAL,
            code=code,
            codes=f"physical_fault:{code}",
            slot_global_tray=3,
            status=STATUS_ESCALATED,
            fault_layer=fault_layer,
        )

    def _load_edge(self, printer_id, incident, *, after=True):
        """The record holds a load NOBODY commanded landing before or after the fault opened."""
        offset = timedelta(seconds=30 if after else -30)
        printer_action._observed_load_at[printer_id] = incident.created_at + offset  # noqa: SLF001

    async def test_an_idle_clean_printer_does_NOT_close_a_physical_hold(self, db_session, printer_factory, monkeypatch):
        """THE 003-H2S PIN. Exactly the constellation the wire lane closes on — and
        the one that laundered a stuck filament three times."""
        printer = await printer_factory()
        await self._physical(db_session, printer.id)
        _wire(monkeypatch, _state("IDLE", []), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 0
        assert await _row(db_session, printer.id) is not None

    async def test_a_completed_load_after_the_fault_closes_it(self, db_session, printer_factory, monkeypatch):
        """Filament moved through the path: a load the AMS carried to completion. The
        printer is idle and the wire is clean, which on its own proves nothing — the
        EDGE (the record's ``observed_load``) is what does."""
        printer = await printer_factory()
        incident = await self._physical(db_session, printer.id)
        self._load_edge(printer.id, incident)
        _wire(monkeypatch, _state("IDLE", []), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 1
        assert await _closed_source(db_session, printer.id) == RESOLVE_REPAIR_OBSERVED

    async def test_a_load_edge_BEFORE_the_fault_is_not_evidence(self, db_session, printer_factory, monkeypatch):
        """The load that preceded the fault is the one that JAMMED."""
        printer = await printer_factory()
        incident = await self._physical(db_session, printer.id)
        self._load_edge(printer.id, incident, after=False)
        _wire(monkeypatch, _state("IDLE", []), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 0
        assert await _row(db_session, printer.id) is not None

    async def test_a_re_stall_after_the_load_keeps_the_hold(self, db_session, printer_factory, monkeypatch):
        """Evidence is not a latch: the wire must ALSO be clean right now."""
        printer = await printer_factory()
        incident = await self._physical(db_session, printer.id)
        self._load_edge(printer.id, incident)
        _wire(monkeypatch, _state("IDLE", [_ptfe_breakage_hms()]), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 0
        assert await _row(db_session, printer.id) is not None

    @staticmethod
    def _running(tray_now: int, *, layer: int = 10):
        """A RUNNING push whose ACTIVE extruder reads ``tray_now`` (single nozzle), on the printer's
        live session at ``layer`` — what the feed state reads FED (or, on 255, not)."""
        state = _state("RUNNING", [])
        state.tray_now = tray_now
        state.layer_num = layer
        return state

    async def test_a_print_fed_past_the_fault_layer_closes_it(self, db_session, printer_factory, monkeypatch):
        """The second evidence: the print FEEDS through the path (RUNNING, the active extruder fed from
        an AMS tray — K12) AND has printed a layer past the one the fault stopped it at. The layer
        advance is the event; nothing waits out a dwell."""
        printer = await printer_factory()
        await self._physical(db_session, printer.id, fault_layer=5)
        _wire(monkeypatch, self._running(1, layer=10), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 1
        assert await _closed_source(db_session, printer.id) == RESOLVE_REPAIR_OBSERVED

    async def test_a_print_feeding_at_the_fault_layer_keeps_it(self, db_session, printer_factory, monkeypatch):
        """Fed, RUNNING — but still at the layer the fault stopped it at: no layer has run through the
        path since the fault yet. It closes on the next layer, not on a clock."""
        printer = await printer_factory()
        await self._physical(db_session, printer.id, fault_layer=10)
        _wire(monkeypatch, self._running(1, layer=10), printer.id)
        assert await spool_recovery.sweep_open_incidents() == 0
        assert await _row(db_session, printer.id) is not None

        _wire(monkeypatch, self._running(1, layer=11), printer.id)
        assert await spool_recovery.sweep_open_incidents() == 1

    async def test_a_print_running_on_air_does_NOT_close_it(self, db_session, printer_factory, monkeypatch):
        """011-H2S / 014-H2S 2026-10-09/10: a print resumed onto an EMPTY toolhead read RUNNING,
        and a RUNNING print was "a print through the path" — the hold closed over air. A print
        laying down nothing moves no filament through anything (K12)."""
        printer = await printer_factory()
        await self._physical(db_session, printer.id)
        _wire(monkeypatch, self._running(255), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 0
        assert await spool_recovery.reevaluate_session(printer.id) == 0
        assert await _row(db_session, printer.id) is not None

    async def test_an_eject_sweep_running_is_NOT_evidence(self, db_session, printer_factory, monkeypatch):
        """An eject is filament-LESS: the toolhead crossing the plate says nothing
        about whether filament can be fed. The plate-occupancy authority is asked
        because it is the one owner of "is an eject running on this printer"."""
        from datetime import datetime, timezone

        from backend.app.services.plate_occupancy import EscalationOnly, PendingEject, plate_occupancy

        printer = await printer_factory()
        await self._physical(db_session, printer.id)
        _wire(monkeypatch, self._running(1), printer.id)  # fed past the fault, so the eject is the one disqualifier
        plate_occupancy.reset_for_tests()
        try:
            plate_occupancy.hydrate_plate(printer.id, "task-0", EscalationOnly())
            plate_occupancy.hydrate_eject(
                printer.id,
                PendingEject(
                    purpose="manual",
                    run_id=None,
                    queue_item_id=None,
                    dispatched_at=datetime.now(timezone.utc),
                    started_at=None,
                    hydrated=True,
                ),
            )

            assert await spool_recovery.sweep_open_incidents() == 0
            assert await _row(db_session, printer.id) is not None
        finally:
            plate_occupancy.reset_for_tests()

    async def test_a_report_off_the_current_session_is_never_evidence(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        incident = await self._physical(db_session, printer.id)
        self._load_edge(printer.id, incident)
        _wire(monkeypatch, _state("IDLE", [], fresh=False), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 0
        assert await _row(db_session, printer.id) is not None

    async def test_an_EXTERNAL_holder_physical_fault_still_closes_on_the_wire(
        self, db_session, printer_factory, monkeypatch
    ):
        """``07FF_C011`` is a firmware PROMPT on the spool holder: the human presses
        Continue on the screen and the code clears, so the wire IS their
        return-to-normal. All 8 physical rows ever closed ``wire_clear`` on this farm
        were codes of exactly this family."""
        printer = await printer_factory()
        await self._physical(db_session, printer.id, code="07FF_C011")
        _wire(monkeypatch, _state("IDLE", []), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 1
        assert await _closed_source(db_session, printer.id) == "wire_clear"

    async def test_a_restart_with_a_stale_tray_level_keeps_the_hold(self, db_session, printer_factory, monkeypatch):
        """``tray_now`` reading a slot is a LEVEL, not an edge: 003-H2S read ``tray_now=1`` before,
        during and after its fault. An idle printer printed nothing past the fault."""
        printer = await printer_factory()
        await self._physical(db_session, printer.id)
        state = _state("IDLE", [])
        state.tray_now = 1
        _wire(monkeypatch, state)

        assert await spool_recovery.reevaluate_session(printer.id) == 0
        assert await _row(db_session, printer.id) is not None

    async def test_the_close_evidence_survives_a_restart(self, db_session, printer_factory, monkeypatch):
        """The motion ledger that held "a print ran through the path after the fault" was process
        memory: a restart emptied it and the hold stood until this process happened to see the path run
        again. The evidence is now the JOB's own record — its layer past the row's ``fault_layer`` — so a
        fresh process (every module's state reset, the feed state re-attached) reads the same answer on
        the session's first fresh report."""
        from backend.app.services.feed_state import JobBoundary

        printer = await printer_factory()
        await self._physical(db_session, printer.id, fault_layer=5)
        spool_recovery._reset_state()  # the restart: process memory gone, the row durable
        state = self._running(1, layer=40)
        feed = ScriptedFeed()
        feed.push(state, boundary=JobBoundary.ATTACH)  # a fresh client ATTACHES to the running job
        feed.push(state)
        client = SimpleNamespace(feed=lambda: feed.reading, job_peaks=lambda: SimpleNamespace(layer_num=40))
        monkeypatch.setattr(spool_recovery.printer_manager, "get_status", lambda _pid: state)
        monkeypatch.setattr(spool_recovery.printer_manager, "get_client", lambda _pid: client)

        assert await spool_recovery.reevaluate_session(printer.id) == 1
        assert await _row(db_session, printer.id) is None

    async def test_a_legacy_row_with_no_fault_layer_waits_for_other_evidence(
        self, db_session, printer_factory, monkeypatch
    ):
        """A row opened before the column existed has no layer to compare against: a feeding print on
        the same job proves nothing about "after the fault" — the record's uncommanded load, the job's
        terminal or Recover answers it."""
        printer = await printer_factory()
        await self._physical(db_session, printer.id, fault_layer=None)
        _wire(monkeypatch, self._running(1, layer=40), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 0
        assert await _row(db_session, printer.id) is not None


class TestAJobPauseCannotOutliveItsJob:
    """The real closer (``sweep_open_incidents``) over a plate-check hold whose job ended
    without the farm seeing its terminal: it closes the row under ``job_ended_unseen`` and drops
    the unit's projection — the row that otherwise had no exit and blocked dispatch on its printer
    for good. The terminal it never saw is the event; one still being handled decides first."""

    async def _plate_check(self, db, printer_id, *, item_id=None):
        from backend.app.models.printer_incident import KIND_PLATE_VISION

        return await _held(db, printer_id, kind=KIND_PLATE_VISION, code="0500_808C", item_id=item_id, job_id="task-1")

    async def test_an_idle_printer_ends_the_hold(self, db_session, printer_factory, monkeypatch):
        from backend.app.models.printer_incident import RESOLVE_JOB_ENDED_UNSEEN

        printer = await printer_factory()
        item = PrintQueueItem(
            printer_id=printer.id,
            status="printing",
            position=1,
            waiting_reason=printer_incidents.WAITING_REASON_PLATE_VISION,
        )
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)
        await self._plate_check(db_session, printer.id, item_id=item.id)
        _wire(monkeypatch, _state("IDLE"), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 1

        assert await _closed_source(db_session, printer.id) == RESOLVE_JOB_ENDED_UNSEEN
        assert (await db_session.get(PrintQueueItem, item.id)).waiting_reason is None
        assert printer_incidents.job_pause_held(printer.id) is False
        assert printer_incidents.hold_blocks_dispatch(printer.id) is False

    async def test_the_plate_terminal_being_handled_decides_first(self, db_session, printer_factory, monkeypatch):
        """The stopped job's own terminal lands within a second of the printer reporting FAILED, and it
        is the one that carries the ``plate_refused`` verdict to the plate authority — the dwell kept it
        first; the in-flight terminal does now."""
        printer = await printer_factory()
        await self._plate_check(db_session, printer.id)
        _wire(monkeypatch, _state("FAILED"), printer.id)
        spool_recovery.printer_manager._terminals_in_flight[printer.id] = 1  # noqa: SLF001

        assert await spool_recovery.sweep_open_incidents() == 0
        assert await _row(db_session, printer.id) is not None

    @pytest.mark.parametrize("live", ["PAUSE", "UNKNOWN", None])
    async def test_a_paused_or_silent_printer_keeps_it(self, db_session, printer_factory, monkeypatch, live):
        """PAUSE on the same job is the hold itself; a silent or absent printer says nothing."""
        printer = await printer_factory()
        await self._plate_check(db_session, printer.id)
        _wire(monkeypatch, _state(live), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 0
        assert await _row(db_session, printer.id) is not None

    async def test_a_driverless_recovering_episode_whose_job_ended_is_closed(
        self, db_session, printer_factory, monkeypatch
    ):
        """``driver_owns`` is class-aware (2026-09-29): a ``recovering`` plate-check episode
        is a promise only a LIVE driver keeps. Read by the promise alone, a row whose job
        ended while no driver lived was skipped by this sweep forever — blocking dispatch
        on its printer. With no driver it is adjudicated like any other job pause."""
        from backend.app.models.printer_incident import KIND_PLATE_VISION, RESOLVE_JOB_ENDED_UNSEEN

        printer = await printer_factory()
        await _held(
            db_session, printer.id, kind=KIND_PLATE_VISION, code="0500_808C", job_id="task-1", status=STATUS_RECOVERING
        )
        _wire(monkeypatch, _state("IDLE"), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 1
        assert await _closed_source(db_session, printer.id) == RESOLVE_JOB_ENDED_UNSEEN
        assert printer_incidents.hold_blocks_dispatch(printer.id) is False

    async def test_a_live_episode_driver_keeps_its_row(self, db_session, printer_factory, monkeypatch):
        """While the driver lives the outcome is its to write — the sweep does not close the row."""
        from backend.app.models.printer_incident import KIND_PLATE_VISION

        class _Live:
            def done(self) -> bool:
                return False

        printer = await printer_factory()
        await _held(
            db_session, printer.id, kind=KIND_PLATE_VISION, code="0500_808C", job_id="task-1", status=STATUS_RECOVERING
        )
        monkeypatch.setitem(printer_incidents._drivers, printer.id, _Live())  # noqa: SLF001
        _wire(monkeypatch, _state("IDLE"), printer.id)

        assert await spool_recovery.sweep_open_incidents() == 0
        assert await _row(db_session, printer.id) is not None

    async def test_the_startup_rearm_never_hands_an_episode_to_the_ams_re_entry(
        self, db_session, printer_factory, monkeypatch
    ):
        """At startup no driver is live, so a ``recovering`` episode is not "owned" — and the
        AMS driver's re-entry, which drives only AMS rows, is never asked to take it."""
        from backend.app.models.printer_incident import KIND_PLATE_VISION

        printer = await printer_factory()
        await _held(
            db_session, printer.id, kind=KIND_PLATE_VISION, code="0500_808C", job_id="task-1", status=STATUS_RECOVERING
        )
        _wire(monkeypatch, _state("PAUSE"))
        reenter = AsyncMock()
        monkeypatch.setattr(spool_recovery, "_reenter_recovering_incident", reenter)

        assert await spool_recovery.reevaluate_session(printer.id) == 0

        reenter.assert_not_awaited()
        assert await _row(db_session, printer.id) is not None

    async def test_the_startup_rearm_leaves_it_for_the_reconcile(self, db_session, printer_factory, monkeypatch):
        """No unseen-end close at startup: the downtime reconcile's synthesised terminal
        classifies a job stopped during the outage as a refused plate only while the row
        is still open."""
        printer = await printer_factory()
        await self._plate_check(db_session, printer.id)
        _wire(monkeypatch, _state("IDLE"))

        assert await spool_recovery.reevaluate_session(printer.id) == 0
        assert await _row(db_session, printer.id) is not None


class TestTheRunoutLevel:
    """A runout refilled while the farm was down left no EDGE for anybody to see — the per-push
    sampler's first sample after a restart only seeds — so the print sat PAUSEd for a button nobody
    pressed. The open runout row's refill is a LEVEL now, read on the live report by the session
    start and the sweep: the wire saying the filament is back resumes it, once, off the record."""

    async def _runout(self, db, printer_id):
        return await printer_incidents.open_new(
            db,
            printer_id=printer_id,
            job_id="task-1",
            item_id=None,
            kind=KIND_RUNOUT,
            code="0700_8011",
            codes="runout:0700_8011@0-3",
            slot_global_tray=3,
            status=STATUS_ESCALATED,
        )

    async def test_a_runout_refilled_across_a_restart_resumes_on_the_first_fresh_report(
        self, db_session, printer_factory, monkeypatch
    ):
        printer = await printer_factory()
        await self._runout(db_session, printer.id)
        spool_recovery._reset_state()  # the restart
        resumed = AsyncMock(return_value=True)
        monkeypatch.setattr(spool_recovery, "_resume_after_refill", resumed)
        # The refill happened during the outage: PAUSEd on the job, the demand CLEARED.
        _wire(monkeypatch, _state("PAUSE", []))

        await spool_recovery.reevaluate_session(printer.id)
        await spool_recovery._resume_tasks[next(iter(spool_recovery._resume_tasks))]

        resumed.assert_awaited_once_with(printer.id, (0, 3))
        assert await _row(db_session, printer.id) is not None  # the printer's answer closes it

    async def test_the_sweep_reads_the_same_level(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        await self._runout(db_session, printer.id)
        resumed = AsyncMock(return_value=True)
        monkeypatch.setattr(spool_recovery, "_resume_after_refill", resumed)
        _wire(monkeypatch, _state("PAUSE", []), printer.id)

        await spool_recovery.sweep_open_incidents()
        await spool_recovery._resume_tasks[next(iter(spool_recovery._resume_tasks))]

        resumed.assert_awaited_once()

    async def test_the_once_is_the_record(self, db_session, printer_factory, monkeypatch):
        """A farm resume on the row already outstanding (or reached, refused, taken over) spends it —
        the level never sends a second."""
        printer = await printer_factory()
        row = await self._runout(db_session, printer.id)
        monkeypatch.setattr(
            spool_recovery.incident_resolution,
            "resume_spent",
            lambda printer_id, incident_id: "the farm resumed this hold once" if incident_id == row.id else None,
        )
        resumed = AsyncMock(return_value=True)
        monkeypatch.setattr(spool_recovery, "_resume_after_refill", resumed)
        _wire(monkeypatch, _state("PAUSE", []), printer.id)

        await spool_recovery.sweep_open_incidents()

        resumed.assert_not_awaited()
        assert spool_recovery._resume_tasks == {}

    async def test_an_absent_demand_under_the_reboot_prompt_is_no_refill(
        self, db_session, printer_factory, monkeypatch
    ):
        """Printer 8, 2026-09-04: a reboot wipes the HMS list, so "no demand" proves nothing while the
        power-loss prompt stands — the level resumes nothing there."""
        printer = await printer_factory()
        await self._runout(db_session, printer.id)
        resumed = AsyncMock(return_value=True)
        monkeypatch.setattr(spool_recovery, "_resume_after_refill", resumed)
        prompt = HMSError(code="8007", attr=0x03000000, module=3, severity=3, full_code="0300000000008007")
        _wire(monkeypatch, _state("PAUSE", [prompt]), printer.id)

        await spool_recovery.sweep_open_incidents()

        resumed.assert_not_awaited()
