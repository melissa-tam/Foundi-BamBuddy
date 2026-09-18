"""Regression test for the PrintLogEntry → PrintArchive backfill migration (#1390).

The migration that added `print_log_entries.archive_id` / `cost` /
`energy_kwh` / `energy_cost` left every pre-existing row NULL, so Quick Stats
read 0 filament cost and an empty time accuracy for everything printed before
the upgrade. The same `run_migrations` pass now backfills them:

  Step 1: link old log entries to their archive via print_name + printer_id.
  Step 2: copy archive.cost / energy_kwh / energy_cost onto the LATEST
          matching log entry per archive.

Only the latest run is credited because `archive.cost` held that run's value —
a reprint overwrote it — so the sum across archives reproduces the pre-upgrade
total exactly. Earlier reprints stay NULL, which is also the live write path's
convention for new prints, so reruns never double-count.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from backend.app.core.database import run_migrations
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")


@pytest.fixture
async def engine_with_legacy_data():
    """Fresh schema + a legacy-shape dataset: two archives, four PrintLogEntry
    rows. The cube.3mf archive carries cost+energy (the reprinted file);
    gear.3mf has neither set. Three matching log entries simulate cube's
    reprint history (status: failed → completed → completed). All log entries
    start with archive_id and cost = NULL, exactly like the column-add
    migration leaves on an upgrading install."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app.models.archive import PrintArchive

    engine = await create_memory_engine()

    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    async with SessionLocal() as session:
        session.add(
            PrintArchive(
                id=1,
                filename="cube.3mf",
                file_path="/x/cube.3mf",
                file_size=100,
                print_name="cube.3mf",
                printer_id=1,
                cost=4.25,
                energy_kwh=0.42,
                energy_cost=0.063,
                status="completed",
            )
        )
        session.add(
            PrintArchive(
                id=2,
                filename="gear.3mf",
                file_path="/x/gear.3mf",
                file_size=100,
                print_name="gear.3mf",
                printer_id=1,
                status="completed",
            )
        )
        await session.commit()

    async with engine.begin() as conn:
        # Three log entries for cube.3mf (two early reprints + a latest run),
        # one for gear.3mf.
        base = datetime.now(timezone.utc) - timedelta(days=10)
        for i, (delta_days, status, print_name) in enumerate(
            [
                (0, "failed", "cube.3mf"),
                (1, "completed", "cube.3mf"),
                (2, "completed", "cube.3mf"),  # latest run for cube — must receive backfill
                (3, "completed", "gear.3mf"),
            ],
            start=1,
        ):
            ts = (base + timedelta(days=delta_days)).isoformat()
            await conn.execute(
                text("""
                    INSERT INTO print_log_entries
                        (id, print_name, printer_id, status, started_at, completed_at,
                         duration_seconds, filament_used_grams, created_at)
                    VALUES (:id, :pn, 1, :status, :ts, :ts, 3600, 25.0, :ts)
                """),
                {"id": i, "pn": print_name, "status": status, "ts": ts},
            )

        # Redundant against create_all, which already leaves these NULL — stated
        # explicitly so the pre-migration state the fixture claims is unmissable.
        await conn.execute(
            text("UPDATE print_log_entries SET archive_id = NULL, cost = NULL, energy_kwh = NULL, energy_cost = NULL")
        )

    yield engine
    await engine.dispose()


async def test_backfill_links_log_entries_to_their_archive(engine_with_legacy_data):
    """All four entries should pick up archive_id after the migration runs."""
    async with engine_with_legacy_data.begin() as conn:
        await run_migrations(conn)

    async with engine_with_legacy_data.connect() as conn:
        result = await conn.execute(text("SELECT id, print_name, archive_id FROM print_log_entries ORDER BY id"))
        rows = result.all()

    assert rows == [
        (1, "cube.3mf", 1),
        (2, "cube.3mf", 1),
        (3, "cube.3mf", 1),
        (4, "gear.3mf", 2),
    ]


async def test_backfill_copies_cost_and_energy_to_latest_run_only(engine_with_legacy_data):
    """Archive cost/energy lands on the latest matching run only; earlier runs stay
    NULL, so summing across runs reproduces the sum of archive costs exactly."""
    async with engine_with_legacy_data.begin() as conn:
        await run_migrations(conn)

    async with engine_with_legacy_data.connect() as conn:
        result = await conn.execute(text("SELECT id, cost, energy_kwh, energy_cost FROM print_log_entries ORDER BY id"))
        rows = result.all()

    # Two earlier cube runs (id 1, 2): cost stays NULL.
    assert rows[0] == (1, None, None, None)
    assert rows[1] == (2, None, None, None)
    # Latest cube run (id 3): receives archive 1's cost / energy.
    assert rows[2] == (3, 4.25, 0.42, 0.063)
    # gear run (id 4): archive 2 has no cost/energy so log stays NULL too.
    assert rows[3] == (4, None, None, None)


async def test_backfill_is_idempotent(engine_with_legacy_data):
    """Running the migration twice produces the same state — no double-backfill,
    no values pulled off rows the second pass would mistakenly treat as 'new'."""
    async with engine_with_legacy_data.begin() as conn:
        await run_migrations(conn)
    async with engine_with_legacy_data.begin() as conn:
        await run_migrations(conn)

    async with engine_with_legacy_data.connect() as conn:
        result = await conn.execute(text("SELECT id, archive_id, cost FROM print_log_entries ORDER BY id"))
        rows = result.all()

    assert rows == [
        (1, 1, None),
        (2, 1, None),
        (3, 1, 4.25),
        (4, 2, None),
    ]


async def test_backfill_skips_archives_with_any_costed_run(engine_with_legacy_data):
    """If ANY log entry for an archive already has cost set — the live write path
    filled it for a new run — the backfill leaves the entire archive alone.

    "Cost is accounted for somewhere in this archive's history" is the migration's
    idempotency anchor; injecting the archive-level value onto another row would
    double-count as soon as the live writes add up.
    """
    async with engine_with_legacy_data.begin() as conn:
        # Pretend run #1 was written post-fix with its own cost.
        await conn.execute(text("UPDATE print_log_entries SET cost = 1.11 WHERE id = 1"))
        await run_migrations(conn)

    async with engine_with_legacy_data.connect() as conn:
        result = await conn.execute(text("SELECT id, cost FROM print_log_entries ORDER BY id"))
        rows = result.all()

    # Run #1 keeps its live-written cost. The archive already has a costed
    # run, so the migration does NOT inject archive.cost onto run #3.
    # gear.3mf (archive 2) still has nothing — but archive.cost is NULL
    # there too, so the backfill UPDATE would set NULL → NULL anyway, which
    # is the desired no-op.
    assert dict(rows) == {1: 1.11, 2: None, 3: None, 4: None}
