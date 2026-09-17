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
"""

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
from backend.app.services import incident_resolution, printer_incidents, spool_recovery
from backend.app.services.bambu_mqtt import HMSError, PrinterState

pytestmark = pytest.mark.asyncio

_DWELL = spool_recovery._HOLD_OVER_DWELL_S


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


def _state(live: str | None, hms: list | None = None) -> PrinterState | None:
    if live is None:
        return None
    st = PrinterState()
    st.state = live
    st.subtask_id = "task-1"
    st.hms_errors = hms or []
    return st


def _wire(monkeypatch, state, *, connected: bool = True) -> None:
    monkeypatch.setattr(spool_recovery.printer_manager, "get_status", lambda _pid: state)
    monkeypatch.setattr(spool_recovery.printer_manager, "is_connected", lambda _pid: connected)


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
                incident,
                "startup",
                incident_resolution.Context(state=state, ledger=incident_resolution.ledger, driver_live=False),
            ).close
            is expected
        )

        # ...and the rearm that consumes it, on the identical evidence.
        assert (await spool_recovery.rearm_incidents_on_startup() == 1) is expected
        assert (await _row(db_session, printer.id) is None) is expected


class TestWireClearSweep:
    async def test_escalated_incident_on_idle_fault_free_printer_closes_after_dwell(
        self, db_session, printer_factory, monkeypatch
    ):
        """LIVENESS — the exact #60 shape, and the one case that fails on the
        2026-08-29 build: escalated physical hold, printer connected, IDLE, wire
        clean. It closes, the queue token goes with it, and the CHIP clears."""
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
        _wire(monkeypatch, _state("IDLE", []))

        assert await spool_recovery.sweep_open_incidents(now=0.0) == 0  # first sighting seeds the dwell
        assert await spool_recovery.sweep_open_incidents(now=_DWELL - 1) == 0  # still inside it
        assert await spool_recovery.sweep_open_incidents(now=_DWELL + 1) == 1

        db_session.expunge_all()
        from sqlalchemy import select

        from backend.app.models.printer_incident import PrinterIncident

        row = (
            await db_session.execute(select(PrinterIncident).where(PrinterIncident.printer_id == printer.id))
        ).scalar_one()
        assert row.resolved_at is not None
        assert row.resolve_source == "wire_clear"
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
        _wire(monkeypatch, _state("IDLE", [_feed_fault_hms()]))

        assert await spool_recovery.sweep_open_incidents(now=0.0) == 0
        assert await spool_recovery.sweep_open_incidents(now=_DWELL + 1) == 0
        assert await _row(db_session, printer.id) is not None

    async def test_the_incidents_own_fault_still_standing_keeps_it_open(self, db_session, printer_factory, monkeypatch):
        """The 2026-08-19 shape, held correctly: a fault live on an IDLE printer is a
        REAL hold — idleness is not recovery."""
        printer = await printer_factory()
        await _held(db_session, printer.id)
        _wire(monkeypatch, _state("IDLE", [_ptfe_breakage_hms()]))

        assert await spool_recovery.sweep_open_incidents(now=_DWELL + 1) == 0
        assert await _row(db_session, printer.id) is not None

    async def test_a_paused_printer_is_never_closed(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        await _held(db_session, printer.id, kind=KIND_RUNOUT)
        _wire(monkeypatch, _state("PAUSE", []))

        assert await spool_recovery.sweep_open_incidents(now=0.0) == 0
        assert await spool_recovery.sweep_open_incidents(now=_DWELL + 1) == 0
        assert await _row(db_session, printer.id) is not None

    async def test_a_disconnected_printer_is_never_closed(self, db_session, printer_factory, monkeypatch):
        """A cached state read during a disconnect is a memory, not evidence."""
        printer = await printer_factory()
        await _held(db_session, printer.id)
        _wire(monkeypatch, _state("IDLE", []), connected=False)

        assert await spool_recovery.sweep_open_incidents(now=0.0) == 0
        assert await spool_recovery.sweep_open_incidents(now=_DWELL + 1) == 0
        assert await _row(db_session, printer.id) is not None

    @pytest.mark.parametrize("live", ["", "UNKNOWN", None])
    async def test_an_unreported_state_is_never_closed(self, db_session, printer_factory, monkeypatch, live):
        printer = await printer_factory()
        await _held(db_session, printer.id)
        _wire(monkeypatch, _state(live))

        assert await spool_recovery.sweep_open_incidents(now=_DWELL + 1) == 0
        assert await _row(db_session, printer.id) is not None

    async def test_a_recovering_incident_is_never_closed(self, db_session, printer_factory, monkeypatch):
        """``recovering`` has a live driver (or the restart lane's re-entry); closing
        it from underneath would race the machine that is acting on it."""
        printer = await printer_factory()
        await _held(db_session, printer.id, status=STATUS_RECOVERING)
        _wire(monkeypatch, _state("IDLE", []))

        assert await spool_recovery.sweep_open_incidents(now=0.0) == 0
        assert await spool_recovery.sweep_open_incidents(now=_DWELL + 1) == 0
        assert await _row(db_session, printer.id) is not None
        assert spool_recovery._hold_over_since == {}  # not even tracked

    async def test_the_dwell_restarts_when_the_printer_re_pauses(self, db_session, printer_factory, monkeypatch):
        """The dwell exists because the fault that OPENED #60 was evaluated 78 ms
        after a dispatch, on a printer reading non-PAUSE for an instant. A momentary
        clean reading must never accumulate toward a close."""
        printer = await printer_factory()
        await _held(db_session, printer.id)
        idle = _state("IDLE", [])
        paused = _state("PAUSE", [_ptfe_breakage_hms()])

        _wire(monkeypatch, idle)
        assert await spool_recovery.sweep_open_incidents(now=0.0) == 0  # seed
        _wire(monkeypatch, paused)
        assert await spool_recovery.sweep_open_incidents(now=_DWELL - 1) == 0  # guard broke → dwell dropped
        assert spool_recovery._hold_over_since == {}
        _wire(monkeypatch, idle)
        assert await spool_recovery.sweep_open_incidents(now=_DWELL) == 0  # re-seeds, does NOT inherit
        assert await spool_recovery.sweep_open_incidents(now=2 * _DWELL - 1) == 0
        assert await spool_recovery.sweep_open_incidents(now=2 * _DWELL + 1) == 1

    async def test_dwell_state_is_pruned_when_the_incident_closes_elsewhere(
        self, db_session, printer_factory, monkeypatch
    ):
        """The dwell dict is keyed by incident id and must not outlive the row — the
        rehydrate story for a restart is "start over", and within a process the
        bookkeeping must not grow one entry per incident the farm has ever held."""
        printer = await printer_factory()
        incident = await _held(db_session, printer.id)
        _wire(monkeypatch, _state("IDLE", []))
        await spool_recovery.sweep_open_incidents(now=0.0)
        assert incident.id in spool_recovery._hold_over_since

        # Somebody else closes it (a resume, a terminal, an operator).
        await printer_incidents.close_open_for_printer(db_session, printer.id, source="terminal")
        await spool_recovery.sweep_open_incidents(now=1.0)
        assert spool_recovery._hold_over_since == {}

    async def test_one_bad_incident_does_not_abort_the_sweep(self, db_session, printer_factory, monkeypatch):
        """Invariant 10: this runs from the scheduler tick, so nothing in it may
        propagate — and one unhealthy row must not shield the rest of the fleet."""
        bad = await printer_factory()
        good = await printer_factory()
        await _held(db_session, bad.id)
        await _held(db_session, good.id)
        _wire(monkeypatch, _state("IDLE", []))

        real_close = printer_incidents.close
        bad_incident = await printer_incidents.get_open(db_session, bad.id)

        async def _boom(db, incident_id, **kwargs):
            if incident_id == bad_incident.id:
                raise RuntimeError("boom")
            return await real_close(db, incident_id, **kwargs)

        with patch.object(printer_incidents, "close", new=AsyncMock(side_effect=_boom)):
            await spool_recovery.sweep_open_incidents(now=0.0)
            closed = await spool_recovery.sweep_open_incidents(now=_DWELL + 1)

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

    async def _physical(self, db, printer_id, *, code="0700_8004", job_id="task-0"):
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
        )

    def _load_edge(self, printer_id, incident, *, after=True):
        """Stamp a completed-load edge before or after the fault opened."""
        from datetime import timedelta

        offset = timedelta(seconds=30 if after else -30)
        incident_resolution.ledger._load_completed_at[printer_id] = incident.created_at + offset  # noqa: SLF001

    async def test_an_idle_clean_printer_does_NOT_close_a_physical_hold(self, db_session, printer_factory, monkeypatch):
        """THE 003-H2S PIN. Exactly the constellation the wire lane closes on — and
        the one that laundered a stuck filament three times."""
        printer = await printer_factory()
        await self._physical(db_session, printer.id)
        _wire(monkeypatch, _state("IDLE", []))

        assert await spool_recovery.sweep_open_incidents(now=0.0) == 0
        assert await spool_recovery.sweep_open_incidents(now=_DWELL + 1) == 0
        assert await _row(db_session, printer.id) is not None

    async def test_a_completed_load_after_the_fault_closes_it_after_the_dwell(
        self, db_session, printer_factory, monkeypatch
    ):
        """Filament moved through the path: a load the AMS carried to completion. The
        printer is idle and the wire is clean, which on its own proves nothing — the
        EDGE is what does."""
        printer = await printer_factory()
        incident = await self._physical(db_session, printer.id)
        self._load_edge(printer.id, incident)
        _wire(monkeypatch, _state("IDLE", []))

        assert await spool_recovery.sweep_open_incidents(now=0.0) == 0  # seeds the dwell
        assert await spool_recovery.sweep_open_incidents(now=_DWELL + 1) == 1

        db_session.expunge_all()
        from sqlalchemy import select

        from backend.app.models.printer_incident import PrinterIncident

        row = (
            await db_session.execute(select(PrinterIncident).where(PrinterIncident.printer_id == printer.id))
        ).scalar_one()
        assert row.resolve_source == RESOLVE_REPAIR_OBSERVED

    async def test_a_load_edge_BEFORE_the_fault_is_not_evidence(self, db_session, printer_factory, monkeypatch):
        """The load that preceded the fault is the one that JAMMED."""
        printer = await printer_factory()
        incident = await self._physical(db_session, printer.id)
        self._load_edge(printer.id, incident, after=False)
        _wire(monkeypatch, _state("IDLE", []))

        assert await spool_recovery.sweep_open_incidents(now=_DWELL + 1) == 0
        assert await _row(db_session, printer.id) is not None

    async def test_a_re_stall_after_the_load_keeps_the_hold(self, db_session, printer_factory, monkeypatch):
        """Evidence is not a latch: the wire must ALSO be clean right now."""
        printer = await printer_factory()
        incident = await self._physical(db_session, printer.id)
        self._load_edge(printer.id, incident)
        _wire(monkeypatch, _state("IDLE", [_ptfe_breakage_hms()]))

        assert await spool_recovery.sweep_open_incidents(now=_DWELL + 1) == 0
        assert await _row(db_session, printer.id) is not None

    async def test_a_running_print_with_no_eject_closes_it(self, db_session, printer_factory, monkeypatch):
        """The second evidence: a print is running THROUGH the path."""
        printer = await printer_factory()
        await self._physical(db_session, printer.id)
        _wire(monkeypatch, _state("RUNNING", []))

        assert await spool_recovery.sweep_open_incidents(now=0.0) == 0
        assert await spool_recovery.sweep_open_incidents(now=_DWELL + 1) == 1

    async def test_an_eject_sweep_running_is_NOT_evidence(self, db_session, printer_factory, monkeypatch):
        """An eject is filament-LESS: the toolhead crossing the plate says nothing
        about whether filament can be fed. The plate-occupancy authority is asked
        because it is the one owner of "is an eject running on this printer"."""
        from datetime import datetime, timezone

        from backend.app.services.plate_occupancy import EscalationOnly, PendingEject, plate_occupancy

        printer = await printer_factory()
        await self._physical(db_session, printer.id)
        _wire(monkeypatch, _state("RUNNING", []))
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

            assert await spool_recovery.sweep_open_incidents(now=0.0) == 0
            assert await spool_recovery.sweep_open_incidents(now=_DWELL + 1) == 0
            assert await _row(db_session, printer.id) is not None
        finally:
            plate_occupancy.reset_for_tests()

    async def test_a_disconnected_printer_is_never_closed(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        incident = await self._physical(db_session, printer.id)
        self._load_edge(printer.id, incident)
        _wire(monkeypatch, _state("IDLE", []), connected=False)

        assert await spool_recovery.sweep_open_incidents(now=_DWELL + 1) == 0
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
        _wire(monkeypatch, _state("IDLE", []))

        assert await spool_recovery.sweep_open_incidents(now=0.0) == 0
        assert await spool_recovery.sweep_open_incidents(now=_DWELL + 1) == 1

        db_session.expunge_all()
        from sqlalchemy import select

        from backend.app.models.printer_incident import PrinterIncident

        row = (
            await db_session.execute(select(PrinterIncident).where(PrinterIncident.printer_id == printer.id))
        ).scalar_one()
        assert row.resolve_source == "wire_clear"

    async def test_a_restart_with_a_stale_tray_level_keeps_the_hold(self, db_session, printer_factory, monkeypatch):
        """The startup rearm gets no dwell, so its only repair evidence is (b) — a
        RUNNING print. After a restart the edge ledger is EMPTY by construction, and
        ``tray_now`` reading a slot is a LEVEL, not an edge: 003-H2S read
        ``tray_now=1`` before, during and after its fault."""
        printer = await printer_factory()
        await self._physical(db_session, printer.id)
        state = _state("IDLE", [])
        state.tray_now = 1
        _wire(monkeypatch, state)

        assert await spool_recovery.rearm_incidents_on_startup() == 0
        assert await _row(db_session, printer.id) is not None

    async def test_a_restart_onto_a_running_print_closes_it(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        await self._physical(db_session, printer.id)
        _wire(monkeypatch, _state("RUNNING", []))

        assert await spool_recovery.rearm_incidents_on_startup() == 1
        assert await _row(db_session, printer.id) is None


class TestLoadCompletedEdge:
    """``MotionLedger.load_completed_at`` is the repair evidence, and it is an EDGE.

    A LEVEL cannot serve: 003-H2S read ``tray_now == 1`` continuously through the
    whole fault. What the sampler stamps is the transition — the AMS finished a
    filament change onto a real feeder — which is filament demonstrably moving.
    """

    def _sample(self, printer_id, *, tray_now, epoch=1, live="IDLE"):
        state = _state(live, [])
        state.tray_now = tray_now
        state.connection_epoch = epoch
        spool_recovery.note_demand_watch(printer_id, state)

    async def test_the_first_sample_only_seeds(self, db_session, printer_factory):
        printer = await printer_factory()
        self._sample(printer.id, tray_now=1)
        assert incident_resolution.ledger.load_completed_at(printer.id) is None

    async def test_a_transition_onto_a_real_feeder_stamps(self, db_session, printer_factory):
        printer = await printer_factory()
        self._sample(printer.id, tray_now=255)
        self._sample(printer.id, tray_now=1)
        assert incident_resolution.ledger.load_completed_at(printer.id) is not None

    async def test_a_standing_level_never_stamps(self, db_session, printer_factory):
        printer = await printer_factory()
        self._sample(printer.id, tray_now=1)
        self._sample(printer.id, tray_now=1)
        self._sample(printer.id, tray_now=1)
        assert incident_resolution.ledger.load_completed_at(printer.id) is None

    async def test_an_unload_never_stamps(self, db_session, printer_factory):
        """255 is "nothing is feeding" — the opposite of the evidence."""
        printer = await printer_factory()
        self._sample(printer.id, tray_now=1)
        self._sample(printer.id, tray_now=255)
        assert incident_resolution.ledger.load_completed_at(printer.id) is None

    async def test_a_reconnect_cannot_fabricate_the_edge(self, db_session, printer_factory):
        """The same rule the negative edges obey: a new MQTT session re-seeds every
        wire fact at once, so a transition across it is a reading, not an event."""
        printer = await printer_factory()
        self._sample(printer.id, tray_now=255, epoch=1)
        self._sample(printer.id, tray_now=1, epoch=2)
        assert incident_resolution.ledger.load_completed_at(printer.id) is None
