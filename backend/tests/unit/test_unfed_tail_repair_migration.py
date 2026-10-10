"""The one-time repair of the three jobs that printed AIR on 2026-10-09/10 (observed-incidents shape 46).

011-H2S (unit 4135 / archive 3056) and 014-H2S (unit 4087 / archive 3052) ran to FINISH after the
AMS's delayed pull-back emptied the toolhead during a paused jam recovery: both were recorded
``completed`` and charged the whole plate. 014-H2S unit 4289 / archive 3104 was stopped by the
operator (``cancelled``, the true word) but charged as if it had fed through its last layer.
``repair_unfed_tail_20261010`` records the two FINISHes ``failed`` / ``printed_without_filament``
with the forward terminal's ``error_message`` sentence, and re-charges every usage row to the
measured fed extent, crediting each spool its over-charge. The rules live in
``services/unfed_tail_repair.py``; ``core.database.run_migrations`` owns the marker and the savepoint.

Every row below is seeded the way production holds it (ids, statuses, job ids, grams and costs as
the read-only report read them on 2026-10-10). The spools are seeded GROWN past what the report
saw, because they kept printing: the credit must be a relative decrement, never a compare against
the report's absolute weight. What these tests pin: the applied result, the marker (a second boot
is a no-op), per-job drift refusal, the weight-locked / spent / archived skip, the relative and
floored credit with the baseline rule, an install that holds none of these rows, and the whole
repair rolling back on an unexpected failure.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.app.core.database import Base, run_migrations
from backend.app.services import unfed_tail_repair
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")

_MARKER = "repair_unfed_tail_20261010"
_TAG = f"[REPAIR] {_MARKER}:"
_CATEGORY = "printed_without_filament"
# The forward terminal's sentence for these FINISHes, and the message the operator's stop kept.
_MSG_4135 = "Printed without filament from layer 93 of 167"
_MSG_4087 = "Printed without filament from layer 9 of 167"
_DOOR = "[0300_0001] The front door is open."

# Spool weight_used as seeded: each is LARGER than when the report read the incident's charges,
# as production's are by deploy time.
_GROWN = {1165: 900.0, 1048: 927.0, 1184: 1250.0, 1193: 80.0}
# The report's unrounded over-increments (live split minus the split at the fed extent).
_CREDIT_2676 = 84.201287  # spool 1165, archive 3056
_CREDIT_2641 = 333.570227  # spool 1184, archive 3052
_CREDIT_2709 = 8.304563  # spool 1184, archive 3104


@pytest.fixture
async def engine():
    eng = await create_memory_engine()
    yield eng
    await eng.dispose()


async def _insert(engine: AsyncEngine, table: str, **values: object) -> None:
    async with engine.begin() as conn:
        await conn.execute(insert(Base.metadata.tables[table]).values(**values))


async def _seed_production(engine: AsyncEngine, *, spools: dict[int, dict[str, object]] | None = None) -> None:
    """The three jobs' rows as production holds them, plus the four spools they charged.

    A live install was migrated BEFORE these rows were written, so its archives went into the
    ``archive_fts`` external-content index through the insert trigger. Seeding before the first boot
    would leave that index empty, and FTS5 then reports "database disk image is malformed" at the
    update trigger's 'delete' (the replay repair's test documents the same fixture artifact). So the
    first boot runs on the empty schema; it also marks every earlier one-time repair applied, as on
    every real install, so none of them judges this seed. Removing this repair's own marker stands
    for "the build carrying the repair has not booted yet".
    """
    await _boot(engine)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM settings WHERE key = :k"), {"k": _MARKER})
    for printer_id, name in ((9, "011-H2S"), (13, "014-H2S")):
        await _insert(
            engine,
            "printers",
            id=printer_id,
            name=name,
            serial_number=f"SN{printer_id:06d}",
            ip_address=f"192.0.2.{printer_id}",
            access_code="00000000",
        )
    jobs = (
        # (unit, printer, archive, job id, status, archive failure_reason, started, ended)
        (
            4135,
            9,
            3056,
            "515810211",
            "completed",
            None,
            datetime(2026, 10, 9, 3, 40, 16),
            datetime(2026, 10, 9, 23, 44, 25),
        ),
        (
            4087,
            13,
            3052,
            "512918044",
            "completed",
            None,
            datetime(2026, 10, 9, 2, 52, 3),
            datetime(2026, 10, 9, 8, 59, 9),
        ),
        (
            4289,
            13,
            3104,
            "593890429",
            "cancelled",
            "User cancelled",
            datetime(2026, 10, 10, 1, 21, 35),
            datetime(2026, 10, 10, 8, 8, 38),
        ),
    )
    for unit_id, printer_id, archive_id, job_id, status, reason, started, ended in jobs:
        await _insert(
            engine,
            "print_archives",
            id=archive_id,
            printer_id=printer_id,
            filename=f"{archive_id}.3mf",
            file_path=f"archive/{printer_id}/{archive_id}.3mf",
            file_size=5696127,
            print_name="Fast_M12 4-in-1 Drill&Driver_top_surfaces_adjusted.gcode_L1-90_spliced",
            status=status,
            subtask_id=job_id,
            started_at=started,
            completed_at=ended,
            filament_used_grams=436.56,
            total_layers=167,
            cost=5.23 if archive_id == 3052 else 5.24,
            failure_reason=reason,
        )
        await _insert(
            engine,
            "print_queue",
            id=unit_id,
            printer_id=printer_id,
            archive_id=archive_id,
            dispatch_subtask_id=job_id,
            status=status,
            plate_id=1,
            started_at=started,
            completed_at=ended,
            stop_source="operator_ui" if status == "cancelled" else None,
            # NULL on both FINISHes in production; the stop's terminal wrote the printer's words.
            error_message=_DOOR if status == "cancelled" else None,
        )
    for entry_id, archive_id, printer_id, status, grams, cost, reason, created in (
        (2464, 3056, 9, "completed", 436.6, 5.24, None, datetime(2026, 10, 9, 23, 44, 25)),
        (2435, 3052, 13, "completed", 436.6, 5.2299999999999995, None, datetime(2026, 10, 9, 8, 59, 9)),
        (2495, 3104, 13, "cancelled", 419.3, 5.03, "User cancelled", datetime(2026, 10, 10, 8, 8, 38)),
    ):
        await _insert(
            engine,
            "print_log_entries",
            id=entry_id,
            archive_id=archive_id,
            printer_id=printer_id,
            printer_name="011-H2S" if printer_id == 9 else "014-H2S",
            status=status,
            filament_used_grams=grams,
            cost=cost,
            failure_reason=reason,
            created_at=created,
        )
    for spool_id, used in _GROWN.items():
        values: dict[str, object] = {
            "id": spool_id,
            "material": "PETG",
            "subtype": "HF",
            "brand": "Bambu Lab",
            "label_weight": 1000,
            "core_weight": 216,
            "weight_used": used,
            "weight_used_baseline": 0.0,
            "data_origin": "ams_auto",
        }
        values.update((spools or {}).get(spool_id, {}))
        await _insert(engine, "spool", **values)
    for usage_id, spool_id, archive_id, printer_id, grams, cost, percent, status, created in (
        (2641, 1184, 3052, 13, 428.7, 5.14, 43, "completed", datetime(2026, 10, 9, 8, 59, 9)),
        (2642, 1193, 3052, 13, 7.9, 0.09, 1, "completed", datetime(2026, 10, 9, 8, 59, 9)),
        (2676, 1165, 3056, 9, 433.8, 5.21, 43, "completed", datetime(2026, 10, 9, 23, 44, 25)),
        (2677, 1048, 3056, 9, 2.8, 0.03, 0, "completed", datetime(2026, 10, 9, 23, 44, 25)),
        (2709, 1184, 3104, 13, 360.9, 4.33, 36, "cancelled", datetime(2026, 10, 10, 8, 8, 37)),
        (2710, 1193, 3104, 13, 58.4, 0.7, 6, "cancelled", datetime(2026, 10, 10, 8, 8, 37)),
    ):
        await _insert(
            engine,
            "spool_usage_history",
            id=usage_id,
            spool_id=spool_id,
            archive_id=archive_id,
            printer_id=printer_id,
            print_name="Fast_M12_4-in-1_Drill&Driver_top_surfaces_adjusted_L1-90_spliced",
            weight_used=grams,
            cost=cost,
            percent_used=percent,
            status=status,
            created_at=created,
        )


async def _boot(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await run_migrations(conn)


async def _rows(engine: AsyncEngine, table: str, columns: tuple[str, ...], ids: tuple[int, ...]) -> dict[int, tuple]:
    t = Base.metadata.tables[table]
    async with engine.connect() as conn:
        result = await conn.execute(select(t.c.id, *(t.c[c] for c in columns)).where(t.c.id.in_(ids)))
        return {row[0]: tuple(row[1:]) for row in result}


async def _units(engine: AsyncEngine) -> dict[int, tuple]:
    return await _rows(engine, "print_queue", ("status", "error_message"), (4135, 4087, 4289))


async def _archives(engine: AsyncEngine) -> dict[int, tuple]:
    return await _rows(
        engine, "print_archives", ("status", "failure_reason", "filament_used_grams", "cost"), (3056, 3052, 3104)
    )


async def _logs(engine: AsyncEngine) -> dict[int, tuple]:
    return await _rows(
        engine, "print_log_entries", ("status", "failure_reason", "filament_used_grams", "cost"), (2464, 2435, 2495)
    )


async def _usage(engine: AsyncEngine) -> dict[int, tuple]:
    return await _rows(
        engine,
        "spool_usage_history",
        ("weight_used", "cost", "percent_used", "status"),
        (2641, 2642, 2676, 2677, 2709, 2710),
    )


async def _weights(engine: AsyncEngine) -> dict[int, float]:
    rows = await _rows(engine, "spool", ("weight_used",), tuple(_GROWN))
    return {spool_id: row[0] for spool_id, row in rows.items()}


async def _marker_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT COUNT(*) FROM settings WHERE key = :k"), {"k": _MARKER})).scalar()


# (status, failure_reason, filament_used_grams, cost) as production holds them, and as owed: the cost by
# the forward archive-cost formula over the repaired charges and the fed share (the tracked cost, since
# the fed share of the slicer estimate is below the tracked grams on all three).
_CHARGED_ARCHIVES = {
    3056: ("completed", None, 436.56, 5.24),
    3052: ("completed", None, 436.56, 5.23),
    3104: ("cancelled", "User cancelled", 436.56, 5.24),
}
_REPAIRED_ARCHIVES = {
    3056: ("failed", _CATEGORY, 436.56, 4.23),
    3052: ("failed", _CATEGORY, 436.56, 1.23),
    3104: ("cancelled", "User cancelled", 436.56, 4.93),
}
_REPAIRED_UNITS = {4135: ("failed", _MSG_4135), 4087: ("failed", _MSG_4087), 4289: ("cancelled", _DOOR)}
_CHARGED_UNITS = {4135: ("completed", None), 4087: ("completed", None), 4289: ("cancelled", _DOOR)}
_REPAIRED_USAGE = {
    2641: (95.1, 1.14, 10, "failed"),
    2642: (7.9, 0.09, 1, "failed"),
    2676: (349.6, 4.2, 35, "failed"),
    2677: (2.8, 0.03, 0, "failed"),
    2709: (352.5, 4.23, 35, "cancelled"),
    2710: (58.4, 0.7, 6, "cancelled"),
}
_CHARGED_USAGE = {
    2641: (428.7, 5.14, 43, "completed"),
    2642: (7.9, 0.09, 1, "completed"),
    2676: (433.8, 5.21, 43, "completed"),
    2677: (2.8, 0.03, 0, "completed"),
    2709: (360.9, 4.33, 36, "cancelled"),
    2710: (58.4, 0.7, 6, "cancelled"),
}


@pytest.mark.asyncio
async def test_the_repair_records_the_finishes_failed_and_recharges_all_three(engine, caplog):
    await _seed_production(engine)

    with caplog.at_level(logging.INFO):
        await _boot(engine)

    assert await _units(engine) == _REPAIRED_UNITS, (
        "the two FINISHes printed air, in the forward terminal's words; the operator's stop and its message stay"
    )
    assert await _archives(engine) == _REPAIRED_ARCHIVES, (
        "the slicer estimate stays; the cost is the archive-cost formula over the fed share"
    )
    assert await _logs(engine) == {
        2464: ("failed", _CATEGORY, 352.4, 4.23),
        2435: ("failed", _CATEGORY, 103.0, 1.23),
        2495: ("cancelled", "User cancelled", 410.9, 4.93),
    }
    assert await _usage(engine) == _REPAIRED_USAGE
    weights = await _weights(engine)
    assert weights[1165] == pytest.approx(_GROWN[1165] - _CREDIT_2676, abs=1e-9)
    assert weights[1184] == pytest.approx(_GROWN[1184] - _CREDIT_2641 - _CREDIT_2709, abs=1e-9), (
        "two credits on one spool, each relative to the value the previous one left"
    )
    assert weights[1048] == _GROWN[1048], "owed = charged: no credit"
    assert weights[1193] == _GROWN[1193], "owed = charged: no credit"
    assert await _marker_count(engine) == 1

    assert "3 of 3 job(s) repaired, 0 absent, 0 skipped on drift; 3 spool credit(s) applied, 0 skipped" in caplog.text
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and _TAG in r.getMessage()]
    assert (
        f"{_TAG} unit 4135 (printer 9, job '515810211'): status 'completed' -> 'failed', "
        f"error_message None -> '{_MSG_4135}'"
    ) in warnings
    assert any(
        "print-log entry 2435 (archive 3052)" in w
        and "filament_used_grams 436.6 -> 103.0, cost 5.2299999999999995 -> 1.23" in w
        for w in warnings
    ), "every write carries its before-values: the log is the repair's rollback record"
    assert any(
        "spool 1165 credited 84.201287 g for usage 2676 (archive 3056): weight_used 900.0 -> 815.8" in w
        for w in warnings
    )
    assert any("usage 2709 spool 1184 (archive 3104): weight_used 360.9 -> 352.5 g" in w for w in warnings)
    assert any(
        "archive 3104: status 'cancelled' -> 'cancelled', failure_reason 'User cancelled' -> 'User cancelled', "
        "cost 5.24 -> 4.93" in w
        for w in warnings
    ), "the kept job's archive cost is re-priced with its before-value"


@pytest.mark.asyncio
async def test_a_second_boot_is_a_noop_even_over_a_restored_pre_image(engine, caplog):
    """THE reason for the marker: the per-row guards already refuse a second application, but a row
    an operator restored to its old value must not be repaired again, and no spool credited twice."""
    await _seed_production(engine)
    await _boot(engine)
    credited = await _weights(engine)
    async with engine.begin() as conn:
        table = Base.metadata.tables["spool_usage_history"]
        await conn.execute(update(table).where(table.c.id == 2676).values(weight_used=433.8, status="completed"))

    caplog.clear()
    with caplog.at_level(logging.INFO):
        await _boot(engine)

    assert _TAG not in caplog.text, "the marker stands: nothing re-planned, nothing logged"
    assert await _weights(engine) == credited
    assert (await _usage(engine))[2676] == (433.8, 4.2, 35, "completed"), "the operator's value stands"
    assert await _marker_count(engine) == 1, "written once, never duplicated"


_DRIFTS = {
    "unit_status": ("print_queue", 4087, {"status": "failed"}),
    "archive_subtask": ("print_archives", 3052, {"subtask_id": "999999999"}),
    "log_grams": ("print_log_entries", 2435, {"filament_used_grams": 400.0}),
    "usage_grams": ("spool_usage_history", 2642, {"weight_used": 8.0}),
    # Facts no write's WHERE restates: only the pre-write verification sees these move.
    "usage_cost": ("spool_usage_history", 2642, {"cost": 0.5}),
    "archive_failure_reason": ("print_archives", 3052, {"failure_reason": "Filament runout"}),
    "unit_error_message": ("print_queue", 4087, {"error_message": "Printer paused: front door open"}),
    "archive_cost": ("print_archives", 3052, {"cost": 5.0}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", sorted(_DRIFTS))
async def test_a_drifted_job_is_skipped_whole_and_the_others_apply(engine, caplog, drift):
    """A job whose rows moved since the report (an operator's edit, a later lane) can never match
    again: its savepoint discards every write of THAT job, and the other jobs' damage is still
    repaired. 014-H2S #536 drifts here; spool 1184 still takes #558's credit, and only that one."""
    await _seed_production(engine)
    table_name, row_id, values = _DRIFTS[drift]
    async with engine.begin() as conn:
        table = Base.metadata.tables[table_name]
        await conn.execute(update(table).where(table.c.id == row_id).values(**values))
    drifted_unit = {
        "unit_status": ("failed", None),
        "unit_error_message": ("completed", "Printer paused: front door open"),
    }.get(drift, ("completed", None))

    with caplog.at_level(logging.INFO):
        await _boot(engine)

    assert await _units(engine) == {4135: ("failed", _MSG_4135), 4087: drifted_unit, 4289: ("cancelled", _DOOR)}
    archives = await _archives(engine)
    assert archives[3052] == (
        "completed",
        "Filament runout" if drift == "archive_failure_reason" else None,
        436.56,
        5.0 if drift == "archive_cost" else 5.23,
    ), "nothing written for the drifted job"
    assert archives[3056] == _REPAIRED_ARCHIVES[3056] and archives[3104] == _REPAIRED_ARCHIVES[3104]
    logs = await _logs(engine)
    assert logs[2435] == ("completed", None, 400.0 if drift == "log_grams" else 436.6, 5.2299999999999995)
    assert logs[2464] == ("failed", _CATEGORY, 352.4, 4.23)
    usage = await _usage(engine)
    assert usage[2641] == _CHARGED_USAGE[2641]
    assert usage[2642] == {
        "usage_grams": (8.0, 0.09, 1, "completed"),
        "usage_cost": (7.9, 0.5, 1, "completed"),
    }.get(drift, _CHARGED_USAGE[2642])
    assert usage[2676] == _REPAIRED_USAGE[2676] and usage[2709] == _REPAIRED_USAGE[2709]
    weights = await _weights(engine)
    assert weights[1184] == pytest.approx(_GROWN[1184] - _CREDIT_2709, abs=1e-9), (
        "the drifted job's credit is not taken"
    )
    assert weights[1165] == pytest.approx(_GROWN[1165] - _CREDIT_2676, abs=1e-9)
    assert f"{_TAG} skip job '512918044' (printer 13, unit 4087, archive 3052)" in caplog.text
    assert "nothing written for this job" in caplog.text
    assert "2 of 3 job(s) repaired, 0 absent, 1 skipped on drift" in caplog.text
    assert await _marker_count(engine) == 1, "a drift is permanent; retrying it at every boot could never succeed"


@pytest.mark.asyncio
async def test_the_kept_cancelled_job_is_verified_although_its_unit_is_never_written(engine, caplog):
    """014-H2S #558 keeps ``cancelled``, so no write restates its unit's or archive's pre-image: the
    verification alone must refuse it once they moved (here an operator re-marked the unit)."""
    await _seed_production(engine)
    async with engine.begin() as conn:
        table = Base.metadata.tables["print_queue"]
        await conn.execute(update(table).where(table.c.id == 4289).values(status="completed"))

    with caplog.at_level(logging.INFO):
        await _boot(engine)

    assert (await _logs(engine))[2495] == ("cancelled", "User cancelled", 419.3, 5.03), "nothing written for it"
    assert (await _archives(engine))[3104] == _CHARGED_ARCHIVES[3104]
    usage = await _usage(engine)
    assert usage[2709] == _CHARGED_USAGE[2709]
    assert usage[2641] == _REPAIRED_USAGE[2641], "the other jobs still apply"
    assert (await _weights(engine))[1184] == pytest.approx(_GROWN[1184] - _CREDIT_2641, abs=1e-9)
    assert f"{_TAG} skip job '593890429' (printer 13, unit 4289, archive 3104)" in caplog.text


@pytest.mark.asyncio
async def test_a_marker_that_cannot_be_written_takes_every_write_down_with_it(engine, caplog):
    """The marker INSERT rides the SAME savepoint as the writes: when it fails, nothing is half
    applied (no job repaired without its marker, to be judged again by the next boot)."""
    await _seed_production(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"CREATE TRIGGER refuse_unfed_marker BEFORE INSERT ON settings WHEN NEW.key = '{_MARKER}' "
                "BEGIN SELECT RAISE(ABORT, 'marker refused'); END"
            )
        )

    with caplog.at_level(logging.INFO):
        await _boot(engine)  # must NOT raise

    assert await _units(engine) == _CHARGED_UNITS
    assert await _archives(engine) == _CHARGED_ARCHIVES
    assert await _usage(engine) == _CHARGED_USAGE
    assert await _weights(engine) == _GROWN
    assert await _marker_count(engine) == 0
    assert f"{_MARKER} failed and was rolled back" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        {"weight_locked": True},
        {"spent_at": datetime(2026, 10, 10, 9, 0, 0)},
        {"archived_at": datetime(2026, 10, 10, 9, 0, 0)},
    ],
    ids=["weight_locked", "spent", "archived"],
)
async def test_a_locked_spent_or_archived_spool_is_not_credited(engine, caplog, state):
    """Such a spool's weight is no longer the repair's to move (the replay repair's R-charge rule):
    its credit is skipped and its usage rows stay exactly as charged, so its ledger still sums to its
    weight. The job is still repaired: statuses, the print-log grams and the other spools."""
    await _seed_production(engine, spools={1184: state})

    with caplog.at_level(logging.INFO):
        await _boot(engine)

    weights = await _weights(engine)
    assert weights[1184] == _GROWN[1184], "not moved"
    assert weights[1165] == pytest.approx(_GROWN[1165] - _CREDIT_2676, abs=1e-9)
    usage = await _usage(engine)
    assert usage[2641] == _CHARGED_USAGE[2641] and usage[2709] == _CHARGED_USAGE[2709], "left as charged"
    assert usage[2642] == _REPAIRED_USAGE[2642], "a zero-credit row on another spool still follows its run"
    assert await _units(engine) == _REPAIRED_UNITS
    assert (await _logs(engine))[2435] == ("failed", _CATEGORY, 103.0, 1.23), "the run's grams are the run's"
    assert await _archives(engine) == _REPAIRED_ARCHIVES, "the archive's cost is the run's figure too"
    assert f"{_TAG} skip spool 1184 credit 333.570227 g:" in caplog.text
    assert "usage 2641 (archive 3052) is left as charged (428.7 g)" in caplog.text
    assert "3 of 3 job(s) repaired, 0 absent, 0 skipped on drift; 1 spool credit(s) applied, 2 skipped" in caplog.text


@pytest.mark.asyncio
async def test_the_credit_is_relative_floored_at_zero_and_lowers_an_anchored_baseline(engine):
    """Relative to the weight read at boot, never the report's figure. Floored at 0. And the R-charge
    baseline rule: a credit that takes weight_used below an operator's reset baseline lowers the
    baseline by the same grams, because consumption since a reset cannot be negative."""
    await _seed_production(
        engine,
        spools={
            1165: {"weight_used": 200.0, "weight_used_baseline": 150.0},
            1184: {"weight_used": 50.0},
        },
    )

    await _boot(engine)

    spools = await _rows(engine, "spool", ("weight_used", "weight_used_baseline"), (1165, 1184))
    assert spools[1165][0] == pytest.approx(200.0 - _CREDIT_2676, abs=1e-9)
    assert spools[1165][1] == pytest.approx(150.0 - _CREDIT_2676, abs=1e-9), "the reset had anchored the charge"
    assert spools[1184] == (0.0, 0.0), "both credits exceed what the spool holds: floored, never negative"


@pytest.mark.asyncio
async def test_an_install_without_these_rows_writes_nothing_but_the_marker(engine, caplog):
    """A fresh install or a dev copy never held these rows. That is not drift and not worth a
    warning; the marker is still written so the next boot does not look again."""
    with caplog.at_level(logging.INFO):
        await _boot(engine)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and _TAG in r.getMessage()]
    assert warnings == []
    assert f"{_TAG} unit 4135 / archive 3056: absent" in caplog.text
    assert "0 of 3 job(s) repaired, 3 absent, 0 skipped on drift" in caplog.text
    assert await _marker_count(engine) == 1


@pytest.mark.asyncio
async def test_an_unexpected_failure_rolls_back_every_job_and_keeps_the_marker_unwritten(engine, monkeypatch, caplog):
    """Only drift is decided per job. Anything else propagates: the outer savepoint discards the jobs
    already applied, the marker stays unwritten, startup carries on, and the next boot retries."""
    await _seed_production(engine)
    original = unfed_tail_repair._repair_unfed_tail_job
    calls: list[int] = []

    async def _fail_on_second(conn, job, tally):
        calls.append(job.unit_id)
        if len(calls) == 2:
            raise RuntimeError("simulated failure")
        return await original(conn, job, tally)

    monkeypatch.setattr(unfed_tail_repair, "_repair_unfed_tail_job", _fail_on_second)

    caplog.clear()
    with caplog.at_level(logging.INFO):
        await _boot(engine)  # must NOT raise

    assert calls == [4087, 4135], "the first job applied before the second failed"
    assert await _units(engine) == _CHARGED_UNITS
    assert await _archives(engine) == _CHARGED_ARCHIVES
    assert await _usage(engine) == _CHARGED_USAGE, "the first job's writes were rolled back too"
    assert await _weights(engine) == _GROWN
    assert await _marker_count(engine) == 0, "unmarked: a later boot retries"
    assert f"{_MARKER} failed and was rolled back" in caplog.text
    assert f"{_TAG} unit 4087" not in caplog.text, "a rolled-back boot never reads as a repaired one"


def test_the_repair_writes_the_forward_classifier_s_category():
    """The repair's literal is frozen with its rows, but it must be the word the forward terminal
    classifier records for the same outcome, so a reader of ``failure_reason`` sees one category."""
    from backend.app.services.terminal_outcome import PRINTED_WITHOUT_FILAMENT_CATEGORY

    assert unfed_tail_repair.FAILURE_CATEGORY == PRINTED_WITHOUT_FILAMENT_CATEGORY == _CATEGORY


def test_the_repair_writes_the_forward_terminal_s_sentence():
    """The unit's ``error_message`` is the queue row's words for the failure; the repaired FINISHes
    carry exactly what the forward terminal writes for the same outcome, at the measured layer."""
    from backend.app.services.terminal_outcome import _printed_without_filament_sentence

    class _Evidence:
        def __init__(self, first_unfed_layer: int) -> None:
            self.first_unfed_layer = first_unfed_layer
            self.total_layers = 167

    jobs = {job.unit_id: job for job in unfed_tail_repair.UNFED_TAIL_JOBS}
    for unit_id, layer in ((4135, 93), (4087, 9)):
        assert jobs[unit_id].fed_extent == layer
        assert jobs[unit_id].error_message_after == _printed_without_filament_sentence(_Evidence(layer))  # type: ignore[arg-type]
    assert jobs[4289].error_message_after == jobs[4289].error_message_before == _DOOR, "the operator's stop keeps it"
