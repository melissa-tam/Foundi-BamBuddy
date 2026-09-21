"""Tests for the fleet observation recorder (services.fleet_activity).

Two halves, because the module has two jobs that fail in different ways.

:meth:`FleetActivityRecorder.sample_once` is driven with an EXPLICIT ``now`` and an
injected ``gather``, so every property the run-length encoding rests on — a bump that
writes one column, a change that leaves neither gap nor overlap, a stale gap that is
preserved rather than papered over, a clock that steps backwards — is asserted against
a timeline the test chose rather than against a wall clock it has to race.

:func:`gather_observation` is then driven for real against the house accessor stubs:
its whole contract is what it refuses to answer, and "returns None" is a property no
end-to-end test would ever notice going wrong.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from backend.app.models.printer import Printer
from backend.app.models.printer_observation_span import (
    PLATE_PHASE_CLEAR,
    PLATE_PHASE_COOLING,
    PLATE_PHASE_EJECTING,
    PLATE_PHASE_HELD,
    PrinterObservationSpan,
)
from backend.app.services import fleet_activity
from backend.app.services.fleet_activity import (
    FleetActivityRecorder,
    Observation,
    gather_observation,
)

pytestmark = pytest.mark.asyncio

# A fixed naive-UTC timeline. Every ``now`` below is an offset from it, so a failure
# reads as "the span ended 30 s late", not as two opaque timestamps.
T0 = datetime(2026, 9, 21, 8, 0, 0)

# The production cadence and the staleness bound, read from the module so this suite
# cannot drift from the numbers the loop actually uses.
TICK = timedelta(seconds=fleet_activity._SAMPLE_INTERVAL_S)
STALE_AFTER = timedelta(seconds=fleet_activity.STALE_AFTER_S)
PAST_GRACE = fleet_activity._STARTUP_GRACE_S + 1.0


def _obs(**overrides: object) -> Observation:
    """A printing, healthy printer — the baseline every test varies one field of."""
    fields: dict[str, object] = {
        "is_active": True,
        "connected": True,
        "gcode_state": "RUNNING",
        "plate_phase": PLATE_PHASE_CLEAR,
        "quarantined": False,
        "usb_present": True,
        "model_mismatch": False,
    }
    fields.update(overrides)
    return Observation(**fields)  # type: ignore[arg-type]


class _Gather:
    """An injected reading half: one answer per printer, replaced between ticks.

    An answer that is an exception is RAISED — the recorder has to survive a reader
    that throws just as it survives one that returns None, and the two are different
    code paths (the first must not even open a savepoint).
    """

    def __init__(self, answers: dict[int, object]) -> None:
        self.answers = answers
        self.seen: list[tuple[int, bool, float]] = []

    def __call__(self, printer_id: int, is_active: bool, *, uptime_s: float) -> Observation | None:
        self.seen.append((printer_id, is_active, uptime_s))
        answer = self.answers[printer_id]
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[return-value]


@pytest.fixture
def recorder() -> FleetActivityRecorder:
    """A fresh instance — the singleton carries a process-lived start stamp."""
    return FleetActivityRecorder()


async def _mk_printer(db, name: str, *, is_active: bool = True) -> Printer:
    printer = Printer(
        name=name,
        serial_number=f"S{name}",
        ip_address="1.2.3.4",
        access_code="x",
        model="H2S",
        is_active=is_active,
    )
    db.add(printer)
    await db.flush()
    return printer


async def _spans(db, printer_id: int | None = None) -> list[PrinterObservationSpan]:
    stmt = select(PrinterObservationSpan).order_by(PrinterObservationSpan.id)
    if printer_id is not None:
        stmt = stmt.where(PrinterObservationSpan.printer_id == printer_id)
    return list((await db.execute(stmt)).scalars().all())


async def _tick(recorder: FleetActivityRecorder, db, gather: _Gather, *, at: datetime, uptime_s: float = 600.0) -> None:
    await recorder.sample_once(db, now=at, uptime_s=uptime_s, gather=gather)


class TestRunLengthEncoding:
    """One row per unchanged tuple, and the edges between rows are exact."""

    async def test_first_tick_opens_one_span(self, db_session, recorder):
        printer = await _mk_printer(db_session, "RLE1")
        gather = _Gather({printer.id: _obs()})

        await _tick(recorder, db_session, gather, at=T0)

        (span,) = await _spans(db_session)
        assert (span.printer_id, span.started_at, span.last_observed_at, span.ended_at) == (
            printer.id,
            T0,
            T0,
            None,
        )
        assert (span.is_active, span.connected, span.gcode_state, span.plate_phase) == (
            True,
            True,
            "RUNNING",
            PLATE_PHASE_CLEAR,
        )
        assert (span.quarantined, span.usb_present, span.model_mismatch) == (False, True, False)

    async def test_unchanged_tuple_bumps_the_same_row(self, db_session, recorder):
        printer = await _mk_printer(db_session, "RLE2")
        gather = _Gather({printer.id: _obs()})

        await _tick(recorder, db_session, gather, at=T0)
        await _tick(recorder, db_session, gather, at=T0 + TICK)
        await _tick(recorder, db_session, gather, at=T0 + 2 * TICK)

        (span,) = await _spans(db_session)
        assert span.started_at == T0  # the span still knows when the condition began
        assert span.last_observed_at == T0 + 2 * TICK
        assert span.ended_at is None

    async def test_changed_tuple_closes_and_opens_with_neither_gap_nor_overlap(self, db_session, recorder):
        printer = await _mk_printer(db_session, "RLE3")
        gather = _Gather({printer.id: _obs()})
        await _tick(recorder, db_session, gather, at=T0)

        gather.answers[printer.id] = _obs(gcode_state="FINISH", plate_phase=PLATE_PHASE_COOLING)
        await _tick(recorder, db_session, gather, at=T0 + TICK)

        closed, opened = await _spans(db_session, printer.id)
        assert closed.ended_at == T0 + TICK
        assert closed.last_observed_at == T0 + TICK
        assert closed.gcode_state == "RUNNING"
        assert opened.started_at == closed.ended_at, "the two spans must MEET at one instant"
        assert (opened.gcode_state, opened.plate_phase, opened.ended_at) == ("FINISH", PLATE_PHASE_COOLING, None)

    async def test_every_observed_column_is_its_own_span_boundary(self, db_session, recorder):
        """Each column is a separate question, so a change in any one ends the span."""
        printer = await _mk_printer(db_session, "RLE4")
        gather = _Gather({printer.id: _obs()})
        await _tick(recorder, db_session, gather, at=T0)

        changes = [
            {"connected": False},
            {"quarantined": True},
            {"usb_present": False},
            {"usb_present": None},
            {"model_mismatch": True},
            {"plate_phase": PLATE_PHASE_HELD},
        ]
        for index, change in enumerate(changes, start=1):
            gather.answers[printer.id] = _obs(**change)
            await _tick(recorder, db_session, gather, at=T0 + index * TICK)

        spans = await _spans(db_session, printer.id)
        assert len(spans) == len(changes) + 1


class TestGapsAndClocks:
    """Silence is recorded as silence, and a clock that lies moves nothing."""

    async def test_stale_span_closes_at_its_own_last_sample_same_tuple(self, db_session, recorder):
        """A restart is not a stretch of 'still printing' — it is a hole, and stays one."""
        printer = await _mk_printer(db_session, "GAP1")
        gather = _Gather({printer.id: _obs()})
        await _tick(recorder, db_session, gather, at=T0)

        back = T0 + STALE_AFTER + timedelta(seconds=1)
        await _tick(recorder, db_session, gather, at=back)

        closed, opened = await _spans(db_session, printer.id)
        assert closed.ended_at == T0, "the span may never be extended across the silence"
        assert closed.last_observed_at == T0
        assert opened.started_at == back
        assert opened.ended_at is None
        assert opened.started_at - closed.ended_at == STALE_AFTER + timedelta(seconds=1)

    async def test_stale_span_closes_at_its_own_last_sample_different_tuple(self, db_session, recorder):
        printer = await _mk_printer(db_session, "GAP2")
        gather = _Gather({printer.id: _obs()})
        await _tick(recorder, db_session, gather, at=T0)

        back = T0 + STALE_AFTER + timedelta(seconds=30)
        gather.answers[printer.id] = _obs(connected=False, gcode_state=None)
        await _tick(recorder, db_session, gather, at=back)

        closed, opened = await _spans(db_session, printer.id)
        assert closed.ended_at == T0
        assert opened.started_at == back
        assert opened.connected is False

    async def test_a_span_exactly_at_the_bound_is_still_fresh(self, db_session, recorder):
        printer = await _mk_printer(db_session, "GAP3")
        gather = _Gather({printer.id: _obs()})
        await _tick(recorder, db_session, gather, at=T0)

        await _tick(recorder, db_session, gather, at=T0 + STALE_AFTER)

        (span,) = await _spans(db_session, printer.id)
        assert span.ended_at is None
        assert span.last_observed_at == T0 + STALE_AFTER

    async def test_a_backward_clock_step_moves_nothing_backwards(self, db_session, recorder):
        """An NTP correction must never produce ended_at < started_at."""
        printer = await _mk_printer(db_session, "GAP4")
        gather = _Gather({printer.id: _obs()})
        await _tick(recorder, db_session, gather, at=T0)
        await _tick(recorder, db_session, gather, at=T0 + TICK)

        backwards = T0 - timedelta(minutes=5)
        await _tick(recorder, db_session, gather, at=backwards)
        (span,) = await _spans(db_session, printer.id)
        assert span.last_observed_at == T0 + TICK, "a bump may not rewind the span's freshness"

        gather.answers[printer.id] = _obs(quarantined=True)
        await _tick(recorder, db_session, gather, at=backwards)

        closed, opened = await _spans(db_session, printer.id)
        assert closed.ended_at == T0 + TICK
        assert closed.ended_at >= closed.started_at
        assert opened.started_at == T0 + TICK
        assert opened.started_at == closed.ended_at

    async def test_no_honest_reading_writes_nothing_and_bumps_nothing(self, db_session, recorder):
        printer = await _mk_printer(db_session, "GAP5")
        gather = _Gather({printer.id: _obs()})
        await _tick(recorder, db_session, gather, at=T0)

        gather.answers[printer.id] = None
        await _tick(recorder, db_session, gather, at=T0 + TICK)

        (span,) = await _spans(db_session, printer.id)
        assert span.last_observed_at == T0, "a None reading is not evidence the tuple still holds"
        assert span.ended_at is None

    async def test_a_printer_with_no_span_and_no_reading_writes_nothing(self, db_session, recorder):
        printer = await _mk_printer(db_session, "GAP6")
        gather = _Gather({printer.id: None})

        await _tick(recorder, db_session, gather, at=T0, uptime_s=0.0)

        assert await _spans(db_session) == []


class TestRosterChanges:
    """Rows come and go; the history they produced does not."""

    async def test_a_deleted_printers_span_is_closed_and_kept(self, db_session, recorder):
        printer = await _mk_printer(db_session, "ROS1")
        gather = _Gather({printer.id: _obs()})
        await _tick(recorder, db_session, gather, at=T0)
        printer_id = printer.id

        await db_session.delete(printer)
        await db_session.flush()
        await recorder.sample_once(db_session, now=T0 + TICK, uptime_s=600.0, gather=_Gather({}))

        (span,) = await _spans(db_session, printer_id)
        assert span.ended_at == T0 + TICK
        assert span.last_observed_at == T0 + TICK

    async def test_a_deleted_printers_stale_span_closes_at_its_own_last_sample(self, db_session, recorder):
        printer = await _mk_printer(db_session, "ROS2")
        gather = _Gather({printer.id: _obs()})
        await _tick(recorder, db_session, gather, at=T0)
        printer_id = printer.id

        await db_session.delete(printer)
        await db_session.flush()
        await recorder.sample_once(
            db_session, now=T0 + STALE_AFTER + timedelta(seconds=1), uptime_s=600.0, gather=_Gather({})
        )

        (span,) = await _spans(db_session, printer_id)
        assert span.ended_at == T0

    async def test_a_deactivated_printer_is_recorded_from_the_first_tick(self, db_session, recorder, monkeypatch):
        """Out of fleet is a KNOWN fact — it must not wait out the startup grace.

        Driven through the REAL ``gather_observation`` with a process that has just
        started: a deactivated printer has no session BY DESIGN, so the grace window
        that protects a still-dialling printer must not silence it.
        """
        printer = await _mk_printer(db_session, "ROS3", is_active=False)
        _stub_accessors(monkeypatch, statuses={})

        await recorder.sample_once(db_session, now=T0, uptime_s=0.0, gather=gather_observation)

        (span,) = await _spans(db_session, printer.id)
        assert (span.is_active, span.connected, span.gcode_state) == (False, False, None)
        assert span.started_at == T0


class TestFailureIsolation:
    """One printer's bad tick costs that printer its tick, and nothing else."""

    async def test_a_reader_that_raises_leaves_the_other_printers_written(self, db_session, recorder):
        good_a = await _mk_printer(db_session, "ISO1")
        bad = await _mk_printer(db_session, "ISO2")
        good_b = await _mk_printer(db_session, "ISO3")
        gather = _Gather(
            {good_a.id: _obs(), bad.id: RuntimeError("the wire blew up"), good_b.id: _obs(quarantined=True)}
        )

        await _tick(recorder, db_session, gather, at=T0)

        written = {span.printer_id for span in await _spans(db_session)}
        assert written == {good_a.id, good_b.id}

    async def test_a_database_failure_rolls_back_only_its_own_printer(self, db_session, recorder):
        """The SAVEPOINT half: a corrupt VALUE, not a corrupt reader.

        ``plate_phase`` is NOT NULL, so a None there is a genuine engine-level
        IntegrityError raised at the savepoint's flush — the shape a future column, a
        widened vocabulary or a truncation bug would take. Without the per-printer
        savepoint it would abort the whole tick's transaction and lose every other
        printer's row with it.
        """
        good_a = await _mk_printer(db_session, "ISO4")
        bad = await _mk_printer(db_session, "ISO5")
        good_b = await _mk_printer(db_session, "ISO6")
        gather = _Gather({good_a.id: _obs(), bad.id: _obs(plate_phase=None), good_b.id: _obs()})

        await _tick(recorder, db_session, gather, at=T0)

        written = {span.printer_id for span in await _spans(db_session)}
        assert written == {good_a.id, good_b.id}

        # And the session is still usable afterwards: the next tick writes normally.
        gather.answers[bad.id] = _obs()
        await _tick(recorder, db_session, gather, at=T0 + TICK)
        assert {span.printer_id for span in await _spans(db_session)} == {good_a.id, bad.id, good_b.id}

    async def test_a_rolled_back_span_never_costs_the_rest_of_the_tick(self, db_session, recorder):
        """The rollback EXPIRES the span it dirtied — and the tick must not touch it again.

        A savepoint rollback restores the session's snapshot, which expires every
        instance the savepoint modified. Under an AsyncSession a later attribute read on
        such an instance is an implicit lazy refresh from a sync context — it raises
        MissingGreenlet, and if that read happens outside a guard (the orphan sweep) the
        whole tick aborts before its commit and EVERY printer loses the sample. That is
        precisely what the per-printer savepoint exists to prevent, so the tick must
        decide the orphans from values captured before any write.
        """
        printer_a = await _mk_printer(db_session, "ISO7")
        printer_b = await _mk_printer(db_session, "ISO8")
        deleted = await _mk_printer(db_session, "ISO9")
        gather = _Gather({printer_a.id: _obs(), printer_b.id: _obs(), deleted.id: _obs()})
        await _tick(recorder, db_session, gather, at=T0)
        deleted_id = deleted.id
        await db_session.delete(deleted)
        await db_session.flush()

        # A has an OPEN span and its change fails at the DB level: the close flushes,
        # the successor violates NOT NULL, the savepoint rolls back and A's span is
        # expired. B changes normally. C is an orphan the sweep must still close.
        gather.answers[printer_a.id] = _obs(gcode_state="FINISH", plate_phase=None)
        gather.answers[printer_b.id] = _obs(gcode_state="FINISH")
        del gather.answers[deleted.id]
        await _tick(recorder, db_session, gather, at=T0 + TICK)

        # The tick committed: B's new span and C's closure are both on disk.
        b_spans = await _spans(db_session, printer_b.id)
        assert len(b_spans) == 2
        assert b_spans[0].ended_at == T0 + TICK and b_spans[1].gcode_state == "FINISH"
        (orphan,) = await _spans(db_session, deleted_id)
        assert orphan.ended_at == T0 + TICK

        # A is untouched: its span is still open and never learned about this tick.
        (a_span,) = await _spans(db_session, printer_a.id)
        assert a_span.ended_at is None
        assert a_span.last_observed_at == T0
        assert a_span.gcode_state == "RUNNING"

        # And the next tick recovers A without a trace of the failure.
        gather.answers[printer_a.id] = _obs(gcode_state="FINISH")
        gather.answers[printer_b.id] = _obs(gcode_state="FINISH")
        await _tick(recorder, db_session, gather, at=T0 + 2 * TICK)

        a_closed, a_open = await _spans(db_session, printer_a.id)
        assert a_closed.ended_at == T0 + 2 * TICK
        assert a_open.gcode_state == "FINISH" and a_open.ended_at is None


class TestOpenSpanInvariant:
    """At most one open span per printer, whatever sequence of ticks it takes."""

    async def test_one_open_span_per_printer_after_a_long_mixed_sequence(self, db_session, recorder):
        printers = [await _mk_printer(db_session, f"INV{i}") for i in range(4)]
        phases = [PLATE_PHASE_CLEAR, PLATE_PHASE_COOLING, PLATE_PHASE_EJECTING, PLATE_PHASE_HELD]
        rng = random.Random(20260921)
        gather = _Gather({printer.id: _obs() for printer in printers})

        at = T0
        for _ in range(120):
            # A mix of bumps, changes, honest holes and gaps wide enough to go stale.
            at += timedelta(seconds=rng.choice([30, 30, 30, 60, 200, 400]))
            for printer in printers:
                roll = rng.random()
                if roll < 0.15:
                    gather.answers[printer.id] = None
                else:
                    gather.answers[printer.id] = _obs(
                        connected=roll > 0.25,
                        gcode_state=rng.choice(["RUNNING", "FINISH", "IDLE", "PAUSE"]),
                        plate_phase=rng.choice(phases),
                        quarantined=roll > 0.9,
                    )
            await _tick(recorder, db_session, gather, at=at)

        open_counts = (
            await db_session.execute(
                select(PrinterObservationSpan.printer_id, func.count())
                .where(PrinterObservationSpan.ended_at.is_(None))
                .group_by(PrinterObservationSpan.printer_id)
            )
        ).all()
        assert sorted(open_counts) == sorted((printer.id, 1) for printer in printers)

        spans = await _spans(db_session)
        assert len(spans) > len(printers), "the sequence has to have produced real history"
        for span in spans:
            assert span.last_observed_at >= span.started_at
            if span.ended_at is not None:
                assert span.ended_at >= span.started_at


# --------------------------------------------------------------------------- #
# The reading half
# --------------------------------------------------------------------------- #
def _status(**overrides: object) -> SimpleNamespace:
    """A live ``PrinterState`` the way the accessors hand one out."""
    fields: dict[str, object] = {
        "connected": True,
        "connection_epoch": 3,
        "state": "RUNNING",
        "subtask_name": "SKU007_plate_1",
        "sdcard": True,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _stub_accessors(
    monkeypatch: pytest.MonkeyPatch,
    *,
    statuses: dict[int, SimpleNamespace],
    eject_present: bool = False,
    plate_occupied: bool = False,
    active_watch: float | None = None,
    deferred: bool = False,
    quarantined: bool = False,
    model_mismatch: bool = False,
    usb_present: bool | None = None,
) -> None:
    """Point every accessor ``gather_observation`` reads at a chosen answer.

    Patched on the objects the module itself holds, so a future rename of the import
    breaks the test rather than silently stubbing something nothing reads.
    """
    monkeypatch.setattr(fleet_activity.printer_manager, "get_status", lambda pid: statuses.get(pid))
    monkeypatch.setattr(fleet_activity.printer_manager, "is_quarantined", lambda pid: quarantined)
    monkeypatch.setattr(fleet_activity.printer_manager, "is_model_mismatch", lambda pid: model_mismatch)
    monkeypatch.setattr(
        fleet_activity.plate_occupancy,
        "current_view",
        lambda pid: SimpleNamespace(eject_present=eject_present, plate_occupied=plate_occupied),
    )
    monkeypatch.setattr(fleet_activity.eject_cooldown_monitor, "active_watch", lambda pid: active_watch)
    monkeypatch.setattr(fleet_activity.eject_cooldown_monitor, "deferred", lambda pid: deferred)
    monkeypatch.setattr(fleet_activity.usb_storage, "usb_present", lambda pid: usb_present)


class TestGatherObservationRefusals:
    """``None`` means 'no honest reading', and it is the contract's whole point."""

    async def test_never_connected_inside_the_startup_grace_is_no_reading(self, monkeypatch):
        _stub_accessors(monkeypatch, statuses={})
        assert gather_observation(7, True, uptime_s=0.0) is None

    async def test_a_client_that_has_never_completed_a_session_is_no_reading(self, monkeypatch):
        """A dialling client exists but its epoch is still 0 — the transport's own answer."""
        _stub_accessors(monkeypatch, statuses={7: _status(connected=False, connection_epoch=0, state="unknown")})
        assert gather_observation(7, True, uptime_s=10.0) is None

    async def test_past_the_grace_a_silent_printer_is_recorded_as_disconnected(self, monkeypatch):
        _stub_accessors(monkeypatch, statuses={})
        observation = gather_observation(7, True, uptime_s=PAST_GRACE)
        assert observation is not None
        assert (observation.connected, observation.gcode_state, observation.usb_present) == (False, None, None)
        assert observation.is_active is True

    async def test_a_dropped_session_never_flaps_the_tuple_with_its_last_state(self, monkeypatch):
        """A state held over from before the drop is a memory, not an observation."""
        _stub_accessors(
            monkeypatch,
            statuses={7: _status(connected=False, connection_epoch=4, state="RUNNING")},
            plate_occupied=True,
            quarantined=True,
        )
        observation = gather_observation(7, True, uptime_s=PAST_GRACE)
        assert observation is not None
        assert (observation.connected, observation.gcode_state) == (False, None)
        # The SERVER-side facts survive the wire going down — they were never the
        # printer's to report.
        assert (observation.plate_phase, observation.quarantined) == (PLATE_PHASE_HELD, True)

    async def test_connected_but_still_unknown_is_no_reading(self, monkeypatch):
        _stub_accessors(monkeypatch, statuses={7: _status(state="unknown")})
        assert gather_observation(7, True, uptime_s=PAST_GRACE) is None

    async def test_connected_with_no_state_at_all_is_no_reading(self, monkeypatch):
        _stub_accessors(monkeypatch, statuses={7: _status(state="")})
        assert gather_observation(7, True, uptime_s=PAST_GRACE) is None

    async def test_a_deactivated_printer_is_always_a_reading(self, monkeypatch):
        _stub_accessors(monkeypatch, statuses={})
        observation = gather_observation(7, False, uptime_s=0.0)
        assert observation is not None
        assert (observation.is_active, observation.connected, observation.gcode_state) == (False, False, None)

    async def test_an_accessor_that_raises_costs_this_printer_its_tick(self, monkeypatch, caplog):
        _stub_accessors(monkeypatch, statuses={7: _status()})

        def boom(pid: int) -> bool:
            raise RuntimeError("quarantine set is gone")

        monkeypatch.setattr(fleet_activity.printer_manager, "is_quarantined", boom)
        with caplog.at_level("WARNING", logger=fleet_activity.__name__):
            assert gather_observation(7, True, uptime_s=PAST_GRACE) is None
        assert [r for r in caplog.records if "could not be read" in r.getMessage()]


class TestGatherObservationReadings:
    """The one derived axis: what the plate is doing, first match wins."""

    async def test_the_state_word_is_the_printers_own_upper_cased(self, monkeypatch):
        _stub_accessors(monkeypatch, statuses={7: _status(state="finish")})
        observation = gather_observation(7, True, uptime_s=PAST_GRACE)
        assert observation is not None
        assert observation.gcode_state == "FINISH"

    async def test_an_eject_in_flight_reads_ejecting(self, monkeypatch):
        _stub_accessors(monkeypatch, statuses={7: _status()}, eject_present=True)
        observation = gather_observation(7, True, uptime_s=PAST_GRACE)
        assert observation is not None
        assert observation.plate_phase == PLATE_PHASE_EJECTING

    async def test_the_live_job_name_alone_reads_ejecting(self, monkeypatch):
        """A sweep started outside the authority's record is still a sweep."""
        _stub_accessors(monkeypatch, statuses={7: _status(subtask_name="eject_production_item42")})
        observation = gather_observation(7, True, uptime_s=PAST_GRACE)
        assert observation is not None
        assert observation.plate_phase == PLATE_PHASE_EJECTING

    async def test_an_armed_watch_reads_cooling(self, monkeypatch):
        _stub_accessors(monkeypatch, statuses={7: _status()}, active_watch=33.0, plate_occupied=True)
        observation = gather_observation(7, True, uptime_s=PAST_GRACE)
        assert observation is not None
        assert observation.plate_phase == PLATE_PHASE_COOLING

    async def test_a_deferred_watch_reads_held(self, monkeypatch):
        """Cooled, fans retired, the eject withheld under a hold — the thermal work is over."""
        _stub_accessors(monkeypatch, statuses={7: _status()}, active_watch=33.0, deferred=True, plate_occupied=True)
        observation = gather_observation(7, True, uptime_s=PAST_GRACE)
        assert observation is not None
        assert observation.plate_phase == PLATE_PHASE_HELD

    async def test_ejecting_outranks_cooling_which_outranks_held(self, monkeypatch):
        _stub_accessors(
            monkeypatch, statuses={7: _status()}, eject_present=True, active_watch=33.0, plate_occupied=True
        )
        observation = gather_observation(7, True, uptime_s=PAST_GRACE)
        assert observation is not None
        assert observation.plate_phase == PLATE_PHASE_EJECTING

    async def test_an_empty_plate_reads_clear(self, monkeypatch):
        _stub_accessors(monkeypatch, statuses={7: _status()})
        observation = gather_observation(7, True, uptime_s=PAST_GRACE)
        assert observation is not None
        assert observation.plate_phase == PLATE_PHASE_CLEAR

    async def test_usb_and_the_flag_sets_ride_through_unchanged(self, monkeypatch):
        _stub_accessors(
            monkeypatch,
            statuses={7: _status()},
            usb_present=False,
            quarantined=True,
            model_mismatch=True,
        )
        observation = gather_observation(7, True, uptime_s=PAST_GRACE)
        assert observation == Observation(
            is_active=True,
            connected=True,
            gcode_state="RUNNING",
            plate_phase=PLATE_PHASE_CLEAR,
            quarantined=True,
            usb_present=False,
            model_mismatch=True,
        )
