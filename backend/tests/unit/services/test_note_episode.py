"""Tests for the cycle-episode ledger (services.cycle_episodes) and its two hooks.

``note_episode`` is called from inside a cooldown's retirement and an eject's terminal
— two places whose whole contract is that they never raise — so half of this suite is
about what it does with bad input, a dead database and a missing event loop: log, drop,
and return. The other half drives the two REAL measuring owners and asserts the row
they produce carries the same figures their log lines do, because a ledger that
disagrees with the log is worse than no ledger.

The write is awaited deterministically: the spawn is captured and then handed to the
REAL ``spawn_background_task`` at a moment the test chooses, so every assertion waits
for the task rather than sleeping and hoping.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from backend.app.core import tasks as core_tasks
from backend.app.models.farm_cycle_episode import (
    COOLDOWN_VARIANT_FAN_ONLY,
    COOLDOWN_VARIANT_HOLD,
    KIND_COOLDOWN,
    KIND_EJECT,
    FarmCycleEpisode,
)
from backend.app.models.printer import Printer
from backend.app.services import cycle_episodes, farm_policy
from backend.app.services.cycle_episodes import note_episode
from backend.app.services.eject.cooldown_prep import CooldownPrep
from backend.app.services.plate_occupancy import EscalationOnly, Evidence, PendingEject, plate_occupancy

pytestmark = pytest.mark.asyncio

_LOGGER_NAME = "backend.app.services.cycle_episodes"

PRINTER_ID = 7
START = datetime(2026, 9, 21, 8, 0, 0)


@pytest.fixture
def ledger(monkeypatch, own_session_factory):
    """Point the writer's own session at the test engine and hold its scheduling.

    ``_store`` opens ``_database.async_session()`` because neither caller has a session
    to lend; the module reads that attribute at call time, which is what makes it
    patchable here.

    The spawn is DEFERRED rather than faked: :func:`_settle` hands each captured
    coroutine to the real ``spawn_background_task`` and waits for the task. That keeps
    the production helper in the path while letting a test release its own transaction
    first — the writer's second connection cannot insert into SQLite while the caller's
    session still holds the file, which is a property of the harness (one file, two
    connections), not of the code under test.
    """
    monkeypatch.setattr("backend.app.core.database.async_session", own_session_factory, raising=False)
    captured: list[tuple[object, str | None]] = []

    def capturing_spawn(coro, *, name: str | None = None):
        captured.append((coro, name))
        return None

    monkeypatch.setattr(cycle_episodes, "spawn_background_task", capturing_spawn)
    return captured


async def _settle(ledger: list[tuple[object, str | None]]) -> None:
    """Run every write this test produced, through the real helper. No sleeps."""
    if not ledger:
        return
    tasks = [core_tasks.spawn_background_task(coro, name=name) for coro, name in ledger]  # type: ignore[arg-type]
    ledger.clear()
    await asyncio.gather(*tasks)


async def _episodes(db) -> list[FarmCycleEpisode]:
    stmt = select(FarmCycleEpisode).order_by(FarmCycleEpisode.id)
    return list((await db.execute(stmt)).scalars().all())


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _LOGGER_NAME and r.levelno == logging.WARNING]


class TestNoteEpisodeStores:
    """A row per finished episode, in the table's own timestamp convention."""

    async def test_a_cooldown_and_an_eject_land_as_rows(self, db_session, ledger):
        note_episode(
            PRINTER_ID,
            KIND_COOLDOWN,
            started_at=START,
            ended_at=START + timedelta(seconds=412),
            variant=COOLDOWN_VARIANT_HOLD,
        )
        note_episode(
            PRINTER_ID,
            KIND_EJECT,
            started_at=START + timedelta(seconds=412),
            ended_at=START + timedelta(seconds=495),
            expected_s=83.0,
            outcome="completed",
            variant="production",
        )
        await _settle(ledger)

        cooldown, eject = await _episodes(db_session)
        assert (cooldown.printer_id, cooldown.kind, cooldown.variant) == (PRINTER_ID, KIND_COOLDOWN, "hold")
        assert (cooldown.started_at, cooldown.ended_at) == (START, START + timedelta(seconds=412))
        assert cooldown.expected_s is None, "a cooldown has nothing to be measured against"
        assert cooldown.outcome is None
        assert (eject.kind, eject.expected_s, eject.outcome, eject.variant) == (
            KIND_EJECT,
            83.0,
            "completed",
            "production",
        )

    async def test_aware_input_is_converted_and_truncated_to_the_second(self, db_session, ledger):
        started = datetime(2026, 9, 21, 10, 0, 0, 500000, tzinfo=timezone(timedelta(hours=2)))
        note_episode(
            PRINTER_ID,
            KIND_EJECT,
            started_at=started,
            ended_at=started + timedelta(seconds=83, milliseconds=750),
            outcome="completed",
        )
        await _settle(ledger)

        (row,) = await _episodes(db_session)
        # 10:00 at UTC+2 is 08:00 UTC; both ends lose their sub-second part
        # (08:00:00.5 + 83.75 s = 08:01:24.25 → 08:01:24).
        assert row.started_at == datetime(2026, 9, 21, 8, 0, 0)
        assert row.ended_at == datetime(2026, 9, 21, 8, 1, 24)
        assert row.started_at.tzinfo is None and row.ended_at.tzinfo is None

    async def test_a_zero_length_episode_is_still_a_measurement(self, db_session, ledger):
        note_episode(PRINTER_ID, KIND_COOLDOWN, started_at=START, ended_at=START)
        await _settle(ledger)

        (row,) = await _episodes(db_session)
        assert row.started_at == row.ended_at


class TestNoteEpisodeRefuses:
    """Nothing here may reach the caller — a lost row, never a raised one."""

    async def test_an_unknown_kind_is_dropped_with_a_warning(self, db_session, ledger, caplog):
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            note_episode(PRINTER_ID, "warmup", started_at=START, ended_at=START + timedelta(seconds=10))
        await _settle(ledger)

        assert await _episodes(db_session) == []
        assert any("unknown kind" in line for line in _warnings(caplog))

    async def test_an_episode_that_ends_before_it_starts_is_dropped(self, db_session, ledger, caplog):
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            note_episode(PRINTER_ID, KIND_EJECT, started_at=START, ended_at=START - timedelta(seconds=1))
        await _settle(ledger)

        assert await _episodes(db_session) == []
        assert any("before it starts" in line for line in _warnings(caplog))

    async def test_a_malformed_timestamp_never_reaches_the_caller(self, db_session, ledger, caplog):
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            note_episode(PRINTER_ID, KIND_EJECT, started_at=None, ended_at=START)  # type: ignore[arg-type]
        await _settle(ledger)

        assert await _episodes(db_session) == []
        assert any("not recorded" in line for line in _warnings(caplog))

    async def test_a_database_failure_is_a_warning_not_an_exception(self, db_session, ledger, caplog, monkeypatch):
        def broken_session():
            raise RuntimeError("database is gone")

        monkeypatch.setattr("backend.app.core.database.async_session", broken_session, raising=False)
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            note_episode(PRINTER_ID, KIND_EJECT, started_at=START, ended_at=START + timedelta(seconds=83))
            await _settle(ledger)

        assert any("not stored" in line for line in _warnings(caplog))

    async def test_no_running_event_loop_is_a_warning_not_an_exception(self, db_session, caplog):
        """The sync half of the contract, driven where there really is no loop.

        ``CooldownPrep.end()`` is sync and documented never to raise; in production a
        loop is always running under it, but the guard is what makes that a property of
        this module rather than an assumption about its callers.
        """
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            await asyncio.to_thread(
                note_episode,
                PRINTER_ID,
                KIND_EJECT,
                started_at=START,
                ended_at=START + timedelta(seconds=83),
            )

        assert await _episodes(db_session) == []
        assert any("not recorded" in line for line in _warnings(caplog))


class TestCooldownHook:
    """``CooldownPrep.end()`` — one episode per cooling episode, hold vs fan-only."""

    @staticmethod
    def _prep(*, hold: str, elapsed_s: float, outcome: str | None = "released") -> CooldownPrep:
        """A retired-ready prep with NO fan lanes.

        Built directly rather than through ``begin()``: the prep is documented as a
        plain value its caller holds, and ``end()``'s measurement reads exactly five of
        its fields. An empty ``fans`` tuple keeps the actuators (and their MQTT client)
        out of a test that is about the ledger row. ``outcome`` is what the WATCH writes
        just before it retires the prep — see the monitor suite for that wiring.
        """
        return CooldownPrep(
            printer_id=PRINTER_ID,
            hold=hold,  # type: ignore[arg-type]
            hold_z=2.0 if hold == "sent" else None,
            max_z=50.1,
            fans=(),
            release_threshold_c=33.0,
            started_at=time.monotonic() - elapsed_s,
            outcome=outcome,  # type: ignore[arg-type]
        )

    async def test_a_held_cooldown_records_one_episode_with_its_duration(self, db_session, ledger):
        prep = self._prep(hold="sent", elapsed_s=412.0)

        prep.end(fan_off=True)
        await _settle(ledger)

        (row,) = await _episodes(db_session)
        assert (row.printer_id, row.kind, row.variant) == (PRINTER_ID, KIND_COOLDOWN, COOLDOWN_VARIANT_HOLD)
        assert row.expected_s is None
        assert row.outcome == "released"
        assert 411 <= (row.ended_at - row.started_at).total_seconds() <= 413

    @pytest.mark.parametrize("verdict", ["released", "stalled", "cleared"])
    async def test_the_watchs_verdict_is_what_the_row_carries(self, db_session, ledger, verdict):
        """``released`` is a cooldown duration; the other two are episodes that ENDED,
        and the cycle statistics have to be able to tell them apart."""
        prep = self._prep(hold="sent", elapsed_s=300.0, outcome=verdict)

        prep.end(fan_off=True)
        await _settle(ledger)

        (row,) = await _episodes(db_session)
        assert row.outcome == verdict

    async def test_a_cooling_that_named_no_verdict_records_none(self, db_session, ledger):
        """A cancelled watch, an exception, a torn-down session: the duration is real,
        the reason it ended is not known, and NULL says exactly that."""
        prep = self._prep(hold="sent", elapsed_s=300.0, outcome=None)

        prep.end(fan_off=True)
        await _settle(ledger)

        (row,) = await _episodes(db_session)
        assert row.outcome is None

    async def test_a_fan_only_cooldown_is_marked_as_one(self, db_session, ledger):
        """Every ``HoldOutcome`` but ``sent`` ran with the plate where the end block left it."""
        prep = self._prep(hold="skipped:service_hold", elapsed_s=200.0)

        prep.end(fan_off=True)
        await _settle(ledger)

        (row,) = await _episodes(db_session)
        assert row.variant == COOLDOWN_VARIANT_FAN_ONLY

    async def test_a_deferred_cooldowns_second_end_records_nothing_more(self, db_session, ledger, caplog):
        """A service hold retires the actuators when the COOLING ends; the watch's exit
        calls ``end()`` again later. One cooling episode, one row — as with the summary line."""
        prep = self._prep(hold="sent", elapsed_s=412.0)

        with caplog.at_level(logging.INFO, logger="backend.app.services.eject.cooldown_prep"):
            prep.end(fan_off=True)
            prep.end(fan_off=True)
        await _settle(ledger)

        assert len(await _episodes(db_session)) == 1
        summaries = [r.getMessage() for r in caplog.records if "cooldown ended after" in r.getMessage()]
        assert len(summaries) == 1, "the row and the summary line must agree on how many episodes there were"

    async def test_the_row_and_the_summary_line_report_the_same_duration(self, db_session, ledger, caplog):
        prep = self._prep(hold="sent", elapsed_s=412.0)

        with caplog.at_level(logging.INFO, logger="backend.app.services.eject.cooldown_prep"):
            prep.end(fan_off=True)
        await _settle(ledger)

        (row,) = await _episodes(db_session)
        (summary,) = [r.getMessage() for r in caplog.records if "cooldown ended after" in r.getMessage()]
        logged_s = float(summary.split("cooldown ended after ")[1].split(" s")[0])
        assert abs((row.ended_at - row.started_at).total_seconds() - logged_s) <= 1


class TestEjectTerminalHook:
    """``farm_policy.on_terminal`` — the sweep's own measurement, kept."""

    @staticmethod
    async def _mk_printer(db, name: str) -> Printer:
        printer = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model="H2S")
        db.add(printer)
        await db.flush()
        return printer

    @staticmethod
    def _arm(printer_id: int, pending: PendingEject) -> None:
        plate_occupancy.hydrate_plate(printer_id, "SUB-E", EscalationOnly())
        assert plate_occupancy.claim_for_eject(printer_id, pending, Evidence()) is None

    @staticmethod
    def _fake_client(subtask: str | None) -> SimpleNamespace:
        return SimpleNamespace(last_dispatch_subtask_id=subtask)

    async def test_a_completed_sweep_records_the_pendings_own_figures(self, db_session, ledger, caplog):
        printer = await self._mk_printer(db_session, "EPej")
        started_at = datetime.now(timezone.utc) - timedelta(seconds=81)
        self._arm(
            printer.id,
            PendingEject("manual", None, None, expected_runtime_s=83.0, started_at=started_at, start_z=2.0),
        )

        with (
            caplog.at_level(logging.INFO, logger="backend.app.services.farm_policy"),
            patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client(None)),
        ):
            await farm_policy.on_terminal(
                db_session,
                printer.id,
                None,
                "completed",
                completed_subtask_id=None,
                completed_subtask_name=f"eject_manual_p{printer.id}",
            )
        # Release the caller's transaction first: the ledger writes on its OWN
        # connection, and one SQLite file cannot be written from two at once.
        await db_session.commit()
        await _settle(ledger)

        (row,) = await _episodes(db_session)
        assert (row.printer_id, row.kind, row.variant, row.outcome) == (printer.id, KIND_EJECT, "manual", "completed")
        assert row.expected_s == 83.0
        assert row.started_at == started_at.replace(tzinfo=None, microsecond=0)
        # The row and the INFO line are computed from ONE "now", so they agree to the second.
        (runtime_line,) = [r.getMessage() for r in caplog.records if " ran " in r.getMessage()]
        logged_s = float(runtime_line.split(" ran ")[1].split("s (")[0])
        assert abs((row.ended_at - row.started_at).total_seconds() - logged_s) <= 1

    async def test_a_watchdog_stopped_sweep_is_still_a_measured_episode(self, db_session, ledger):
        """Sweep motion complete is not the same as a verified sweep — but it IS a duration,
        and the outcome is what says the job did not finish cleanly."""
        printer = await self._mk_printer(db_session, "EPwd")
        started_at = datetime.now(timezone.utc) - timedelta(seconds=179)
        self._arm(
            printer.id,
            PendingEject(
                "manual",
                None,
                None,
                expected_runtime_s=83.0,
                started_at=started_at,
                runtime_exceeded_at=datetime.now(timezone.utc) - timedelta(seconds=75),
            ),
        )

        with patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client(None)):
            await farm_policy.on_terminal(
                db_session,
                printer.id,
                None,
                "failed",
                completed_subtask_id=None,
                completed_subtask_name=f"eject_manual_p{printer.id}",
            )
        # Release the caller's transaction first: the ledger writes on its OWN
        # connection, and one SQLite file cannot be written from two at once.
        await db_session.commit()
        await _settle(ledger)

        (row,) = await _episodes(db_session)
        assert row.outcome == "failed"
        assert 178 <= (row.ended_at - row.started_at).total_seconds() <= 181
        assert plate_occupancy.is_plate_occupied(printer.id) is True, "the plate still stays gated"

    async def test_a_sweep_the_printer_never_started_records_nothing(self, db_session, ledger):
        """No start echo, no duration — there is nothing to measure and nothing is invented."""
        printer = await self._mk_printer(db_session, "EPns")
        self._arm(printer.id, PendingEject("manual", None, None, expected_runtime_s=83.0))

        with patch.object(farm_policy.printer_manager, "get_client", return_value=self._fake_client(None)):
            await farm_policy.on_terminal(
                db_session,
                printer.id,
                None,
                "failed",
                completed_subtask_id=None,
                completed_subtask_name=f"eject_manual_p{printer.id}",
            )
        # Release the caller's transaction first: the ledger writes on its OWN
        # connection, and one SQLite file cannot be written from two at once.
        await db_session.commit()
        await _settle(ledger)

        assert await _episodes(db_session) == []
