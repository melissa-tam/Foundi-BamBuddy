"""The EVERY-BOOT backfill of the action record for every step that names no action (2026-10-11/12).

``backfill_step_actions`` gives each unlinked ``printer_incident_step`` ONE ``printer_action`` row (its
own facts: printer, command, target, the incident's job, the send time) and links it; the answer comes
from the explicit ``_ANSWER_OF`` table, a reading that means "not answered yet" staying outstanding on
an OPEN row and voided on a CLOSED one. The rules live in ``services/printer_action_backfill.py``;
``core.database.run_migrations`` owns the savepoint and runs it on EVERY boot (no marker): a step the
previous build writes during a rollback window is linked when this build returns, so its pending
command is never invisible (AMS shape 46). What these tests pin: the table covers every reading the
drivers write (and names only the record's commands and answers), the applied result row by row, a
step written AFTER a first boot is linked on the second, a boot with nothing to link writes and logs
nothing, a step already linked is never re-linked, an unmapped reading is left unlinked and logged on
every boot, and an unexpected failure rolls every write back and the next boot retries.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import get_args

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.app.core.database import Base, run_migrations
from backend.app.models import printer_incident as incident_model
from backend.app.services import command_outcome, pause_recovery, printer_action_backfill, printer_incidents
from backend.app.services.spool_recovery import Lever, LeverReading
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")

# The retired once-marker: no longer consulted, and never written.
_RETIRED_MARKER = "backfill_printer_action_20261011"
_TAG = "[MIGRATION] step-action backfill:"
_OPEN, _CLOSED = 501, 502
_SENT = datetime(2026, 10, 10, 6, 0, 0)
_READ = datetime(2026, 10, 10, 6, 0, 40)


@pytest.fixture
async def engine():
    eng = await create_memory_engine()
    yield eng
    await eng.dispose()


async def _boot(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await run_migrations(conn)


async def _insert(engine: AsyncEngine, table: str, **values: object) -> None:
    async with engine.begin() as conn:
        await conn.execute(insert(Base.metadata.tables[table]).values(**values))


# (step id, incident, kind, name, target, feeder, outcome)
_STEPS: tuple[tuple[int, int, str, str, int | None, str | None, str | None], ...] = (
    (1, _OPEN, "command", "unload", None, "jammed", "complete"),
    (2, _OPEN, "command", "load", 3, "empty", "held"),
    (3, _OPEN, "lever", "print_stop", None, None, "stopped"),
    (4, _OPEN, "dialog", "IGNORE_RESUME", None, None, "success"),
    (5, _OPEN, "stop", "retry_stop", None, None, None),
    (6, _OPEN, "dialog", "DBL_CHECK_OK", None, None, "not_sent"),
    (7, _CLOSED, "command", "load", 2, "other", "held"),
    (8, _CLOSED, "stop", "deadline_stop", None, None, "not_taken"),
    (9, _CLOSED, "lever", "resume_then_pause", None, None, "wedged"),
    (10, _CLOSED, "command", "unload", None, "external", "acted"),
)


async def _seed(engine: AsyncEngine) -> None:
    """One printer, an OPEN hold and a CLOSED one, and their pre-record steps (no action).

    The first boot runs on the empty schema and marks every earlier one-time repair applied, as on
    every real install; the steps written after it stand for the previous build's (before the cutover,
    or during a rollback window), which the NEXT boot links."""
    await _boot(engine)
    await _insert(
        engine, "printers", id=7, name="007-H2S", serial_number="SN000007", ip_address="192.0.2.7", access_code="0"
    )
    for incident_id, status in ((_OPEN, incident_model.STATUS_ESCALATED), (_CLOSED, incident_model.STATUS_RESOLVED)):
        await _insert(
            engine,
            "printer_incident",
            id=incident_id,
            printer_id=7,
            job_id=f"job-{incident_id}",
            kind="jam",
            code="0700_8010",
            codes="0700_8010",
            status=status,
            created_at=_SENT,
            resolved_at=_READ if incident_id == _CLOSED else None,
        )
    for step_id, incident_id, kind, name, target, feeder, outcome in _STEPS:
        await _insert(
            engine,
            "printer_incident_step",
            id=step_id,
            incident_id=incident_id,
            seq=step_id,
            kind=kind,
            name=name,
            target=target,
            feeder=feeder,
            outcome=outcome,
            sent_at=_SENT,
            read_at=_READ if outcome is not None else None,
        )


async def _actions_by_step(engine: AsyncEngine) -> dict[int, dict[str, object]]:
    step = Base.metadata.tables["printer_incident_step"]
    action = Base.metadata.tables["printer_action"]
    async with engine.connect() as conn:
        result = await conn.execute(
            select(step.c.id.label("step_id"), action).select_from(step.join(action, action.c.id == step.c.action_id))
        )
        return {row.step_id: {k: v for k, v in row._mapping.items() if k != "step_id"} for row in result}


async def _action_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT COUNT(*) FROM printer_action"))).scalar_one()


async def _marker_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(text("SELECT COUNT(*) FROM settings WHERE key = :k"), {"k": _RETIRED_MARKER})
        ).scalar_one()


class TestTheAnswerTable:
    def test_every_reading_a_driver_writes_has_a_row(self):
        """The table is TOTAL over the drivers' own vocabularies — a new reading is given its row."""
        written = (
            {("command", r) for r in get_args(command_outcome.MotionReading)}
            | {("lever", r) for r in get_args(LeverReading)}
            | {("dialog", r) for r in get_args(pause_recovery.PlateCheckDialogAnswer)}
            | {("stop", r) for r in get_args(pause_recovery.PlateCheckStopAnswer)}
        )
        # ``PlateCheckStopAnswer`` nests ``PlateCheckStopNotTaken``: flatten it.
        flat: set[tuple[str, str]] = set()
        for kind, reading in written:
            args = get_args(reading)
            flat |= {(kind, a) for a in args} if args else {(kind, reading)}
        assert flat == set(printer_action_backfill._ANSWER_OF)

    def test_the_table_names_only_the_record_s_answers(self):
        record = {a.value for a in command_outcome.Answer}
        assert set(printer_action_backfill._ANSWER_OF.values()) <= record | {"open_outstanding"}

    def test_every_lever_maps_onto_a_record_command(self):
        assert set(printer_action_backfill._LEVER_COMMAND) == set(get_args(Lever))
        for command in printer_action_backfill._LEVER_COMMAND.values():
            command_outcome.spec_of(command)  # raises on a name the record does not know
        for kind, name in (("command", "load"), ("command", "unload"), ("dialog", "x"), ("stop", "retry_stop")):
            command_outcome.spec_of(printer_action_backfill._command_of(kind, name))

    def test_the_frozen_literals_are_the_live_vocabulary(self):
        assert printer_action_backfill._CLOSED_STATUSES == (
            incident_model.STATUS_RESOLVED,
            incident_model.STATUS_ABORTED,
        )
        assert set(printer_incidents.PLATE_CHECK_HUMAN_ACTIONS) > printer_action_backfill._HUMAN_ONLY_BUTTONS


async def test_every_pre_record_step_gets_its_action_by_the_table(engine, caplog):
    await _seed(engine)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await _boot(engine)

    actions = await _actions_by_step(engine)
    assert set(actions) == {s[0] for s in _STEPS}
    answers = {step_id: (a["command"], a["answer"], a["reason"]) for step_id, a in actions.items()}
    assert answers == {
        1: ("unload", "reached", None),
        2: ("load", None, None),  # held on an OPEN row: the record answers it on the next event
        3: ("stop", "reached", None),
        4: ("dialog_press", "reached", None),
        5: ("stop", None, None),  # never answered on an OPEN row: outstanding
        6: ("dialog_press", "refused", None),  # never sent: the refusal token, never the printer's words
        7: ("load", "void", "pre-record step: held"),  # nothing can answer it on a CLOSED row
        8: ("stop", "void", "pre-record step: not_taken"),
        9: ("resume", "void", "pre-record step: wedged"),
        10: ("unload", "void", "pre-record step: acted"),
    }
    for step_id, action in actions.items():
        incident_id = next(s[1] for s in _STEPS if s[0] == step_id)
        assert (action["printer_id"], action["incident_id"], action["job_id"]) == (7, incident_id, f"job-{incident_id}")
        assert action["sent_at"] == _SENT
        assert action["answered_at"] == (None if action["answer"] is None else _READ)
    # The step's own facts carry over: a command's target and entry, the held load's ACK, the actor.
    assert (actions[2]["target"], actions[2]["entry"], actions[2]["acked_at"]) == (3, "other_empty", _READ)
    assert (actions[1]["target"], actions[1]["entry"], actions[1]["acked_at"]) == (None, "other_loaded", None)
    assert actions[10]["entry"] == "other_empty"  # the external holder: nothing AMS-fed at the extruder
    assert actions[4]["actor"] == "operator"
    assert actions[6]["refusal"] == "not_sent"
    assert {a["refusal"] for step_id, a in actions.items() if step_id != 6} == {None}
    assert {a["actor"] for step_id, a in actions.items() if step_id != 4} == {"farm"}
    assert await _marker_count(engine) == 0, "the retired once-marker is never written"
    assert f"{_TAG} 10 step(s) linked to a backfilled action (2 left outstanding on an open hold)" in caplog.text


async def test_a_boot_with_nothing_to_link_writes_and_logs_nothing(engine, caplog):
    await _seed(engine)
    await _boot(engine)
    before = await _actions_by_step(engine)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await _boot(engine)
    assert await _actions_by_step(engine) == before
    assert await _action_count(engine) == len(_STEPS)
    assert _TAG not in caplog.text


async def test_a_step_written_after_a_first_boot_is_linked_on_the_second(engine):
    """The rollback window: this build booted and linked everything, the previous build ran and wrote a
    step with no action (it knows no action record), and this build returns — that step is linked, so its
    pending command reads from the record (AMS shape 46). A once-marker left it unlinked forever."""
    await _seed(engine)
    await _boot(engine)
    await _insert(
        engine,
        "printer_incident_step",
        id=11,
        incident_id=_OPEN,
        seq=11,
        kind="command",
        name="load",
        target=1,
        feeder="empty",
        outcome="held",
        sent_at=_SENT,
        read_at=_READ,
    )
    await _boot(engine)
    actions = await _actions_by_step(engine)
    assert set(actions) == {s[0] for s in _STEPS} | {11}
    assert (actions[11]["command"], actions[11]["target"], actions[11]["answer"]) == ("load", 1, None)
    assert actions[11]["incident_id"] == _OPEN
    assert await _action_count(engine) == len(_STEPS) + 1


async def test_a_step_already_linked_is_never_relinked(engine):
    await _seed(engine)
    await _insert(engine, "printer_action", id=900, printer_id=7, actor="farm", command="unload", sent_at=_SENT)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE printer_incident_step SET action_id = 900 WHERE id = 1"))
    await _boot(engine)
    actions = await _actions_by_step(engine)
    assert actions[1]["id"] == 900
    assert await _action_count(engine) == len(_STEPS)  # 9 backfilled + the one already there


async def test_an_unmapped_reading_is_left_unlinked_and_logged(engine, caplog):
    await _seed(engine)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE printer_incident_step SET outcome = 'mystery' WHERE id = 9"))
        await conn.execute(text("UPDATE printer_incident_step SET kind = 'note', name = 'x' WHERE id = 10"))
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await _boot(engine)
    actions = await _actions_by_step(engine)
    assert set(actions) == {s[0] for s in _STEPS} - {9, 10}
    assert f"{_TAG} step 9 (lever='resume_then_pause') outcome 'mystery' has no mapping -- left unlinked" in caplog.text
    assert f"{_TAG} step 10 (note='x') names no record command -- left unlinked" in caplog.text
    assert "2 left unlinked on an outcome with no mapping" in caplog.text
    # Every boot re-reads the unlinked set: the unmapped steps are reported again, and still not guessed.
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await _boot(engine)
    assert set(await _actions_by_step(engine)) == {s[0] for s in _STEPS} - {9, 10}
    assert "2 left unlinked on an outcome with no mapping" in caplog.text
    assert await _action_count(engine) == len(_STEPS) - 2


async def test_an_unexpected_failure_rolls_back_every_write_and_the_next_boot_retries(engine, monkeypatch, caplog):
    await _seed(engine)
    original = printer_action_backfill._entry_of
    calls: list[str] = []

    def _fail_on_fifth(kind, feeder):
        calls.append(kind)
        if len(calls) == 5:
            raise RuntimeError("simulated failure")
        return original(kind, feeder)

    monkeypatch.setattr(printer_action_backfill, "_entry_of", _fail_on_fifth)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await _boot(engine)  # must NOT raise

    assert len(calls) == 5, "four steps were backfilled before the fifth failed"
    assert await _actions_by_step(engine) == {}
    assert await _action_count(engine) == 0, "the first four actions were rolled back too"
    assert f"{_TAG} failed and was rolled back" in caplog.text

    monkeypatch.setattr(printer_action_backfill, "_entry_of", original)
    await _boot(engine)  # the next boot retries
    assert set(await _actions_by_step(engine)) == {s[0] for s in _STEPS}


async def test_a_table_from_an_earlier_build_gains_the_refusal_column(engine):
    """``create_all`` adds ``printer_action.refusal`` to a fresh table only: a DB that ran an earlier build
    of the table (no column) gains it at the next boot, before the backfill writes it."""
    await _boot(engine)
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE printer_action DROP COLUMN refusal"))
        columns = {row[1] for row in (await conn.execute(text("PRAGMA table_info(printer_action)"))).all()}
        assert "refusal" not in columns
    await _boot(engine)
    async with engine.connect() as conn:
        columns = {row[1] for row in (await conn.execute(text("PRAGMA table_info(printer_action)"))).all()}
    assert "refusal" in columns


async def test_a_table_from_an_earlier_build_gains_the_bound_passed_column(engine):
    """``printer_action.bound_passed_at`` (2026-10-12, the self-heal bound): ``create_all`` adds it to a fresh
    table only; a DB that ran an earlier build of the table gains it at the next boot, empty."""
    await _boot(engine)
    await _insert(
        engine, "printers", id=7, name="007-H2S", serial_number="SN000007", ip_address="192.0.2.7", access_code="0"
    )
    await _insert(engine, "printer_action", id=1, printer_id=7, actor="farm", command="stop", sent_at=_SENT)
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE printer_action DROP COLUMN bound_passed_at"))
    await _boot(engine)
    async with engine.connect() as conn:
        columns = {row[1] for row in (await conn.execute(text("PRAGMA table_info(printer_action)"))).all()}
        value = (await conn.execute(text("SELECT bound_passed_at FROM printer_action WHERE id = 1"))).scalar_one()
    assert "bound_passed_at" in columns
    assert value is None


async def test_a_hold_whose_only_resume_was_backfilled_never_sent_is_still_owed(engine):
    """A historical lever resume that never left the farm (``not_sent``) is backfilled ``refused`` WITH the
    refusal token, so it reads as never received: the once (``incident_resolution.resume_spent``) is not
    spent, and an open toolhead hold over a fed toolhead is still owed its resume."""
    from types import SimpleNamespace

    from backend.app.services import incident_resolution
    from backend.app.services.bambu_mqtt import PrinterState
    from backend.app.services.feed_state import FED_PHASES, FeedPhase

    await _boot(engine)
    await _insert(
        engine, "printers", id=7, name="007-H2S", serial_number="SN000007", ip_address="192.0.2.7", access_code="0"
    )
    await _insert(
        engine,
        "printer_incident",
        id=_OPEN,
        printer_id=7,
        job_id="job-501",
        kind="toolhead_refill",
        code="",
        codes="",
        status=incident_model.STATUS_ESCALATED,
        created_at=_SENT,
    )
    await _insert(
        engine,
        "printer_incident_step",
        id=1,
        incident_id=_OPEN,
        seq=1,
        kind="lever",
        name="resume",
        outcome="not_sent",
        sent_at=_SENT,
        read_at=_READ,
    )
    await _boot(engine)

    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(engine, expire_on_commit=False) as db:
        printer_incidents._reset_state()  # noqa: SLF001
        await printer_incidents.rehydrate(db)
    try:
        (resume,) = printer_incidents.farm_actions(7, _OPEN)
        assert (resume.command, resume.answer, resume.unsent, resume.printer_refused) == (
            "resume",
            "refused",
            True,
            False,
        )
        state = PrinterState()
        state.connected = True
        state.state = "PAUSE"
        state.subtask_id = "job-501"
        state.tray_now = 1
        row = SimpleNamespace(id=_OPEN, printer_id=7, kind="toolhead_refill", code="", job_id="job-501")
        ctx = incident_resolution.Context(
            state=state,
            driver_live=False,
            # The reading's ``toolhead_fed`` is the leaf's own set (``feed_state.FED_PHASES``), never re-spelled.
            feed=SimpleNamespace(phase=FeedPhase.FED, toolhead_fed=FeedPhase.FED in FED_PHASES),
        )
        assert incident_resolution.resume_owed(row, ctx).owed is True
    finally:
        printer_incidents._reset_state()  # noqa: SLF001
