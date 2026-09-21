"""The fleet-activity schema: both log tables arrive on a fresh AND on an existing install.

Neither table is created by ``run_migrations``. ``init_db`` runs ``create_all`` and
``run_migrations`` inside ONE transaction, and ``create_all`` creates a table that is
missing whether the database is minutes or months old — so a hand-written CREATE TABLE
would be a second, drifting spelling of the same schema. That claim is exactly what this
file checks: the pre-existing arm below builds a database WITHOUT the two tables and
without the print-log index, brings it up the way a real boot does, and then demands the
same schema the fresh arm gets.

The one statement the migration does carry is the ``print_log_entries.created_at``
index, which every window reader of that append-only table needs.

The open-span exclusivity index gets its own cases: it is the invariant the whole
run-length encoding rests on (one open span per printer, or no reader can tell which row
is current), and it is enforced by the database rather than by the recorder's own
bookkeeping — so it is tested against the database.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from backend.app.core.database import Base, run_migrations
from backend.app.models.farm_cycle_episode import (
    COOLDOWN_VARIANTS,
    EPISODE_KINDS,
    FarmCycleEpisode,
)
from backend.app.models.printer_observation_span import (
    PLATE_PHASE_CLEAR,
    PLATE_PHASES,
    PrinterObservationSpan,
)
from backend.tests._fixtures.db import MEMORY_DATABASE_URL, create_memory_engine, import_all_models

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")

_SPAN_TABLE = "printer_observation_span"
_EPISODE_TABLE = "farm_cycle_episode"
_NEW_TABLES = (_SPAN_TABLE, _EPISODE_TABLE)
_PRINT_LOG_INDEX = "ix_print_log_entries_created_at"

# Spelled out rather than derived from the model: a column silently dropped from the
# model would take a derived expectation with it and this file would still pass.
_SPAN_COLUMNS = {
    "id",
    "printer_id",
    "started_at",
    "last_observed_at",
    "ended_at",
    "is_active",
    "connected",
    "gcode_state",
    "plate_phase",
    "quarantined",
    "usb_present",
    "model_mismatch",
}
_EPISODE_COLUMNS = {
    "id",
    "printer_id",
    "kind",
    "started_at",
    "ended_at",
    "expected_s",
    "outcome",
    "variant",
}

_SPAN_INDEXES = {
    "ux_printer_observation_span_open",
    "ix_printer_observation_span_printer_started",
    "ix_printer_observation_span_ended",
}
_OPEN_SPAN_INDEX = "ux_printer_observation_span_open"
_EPISODE_INDEX = "ix_farm_cycle_episode_kind_ended"

BASE_TIME = datetime(2026, 9, 21, 8, 0, 0)


def _create_all_except_the_new_tables(sync_conn) -> None:
    """``create_all`` for every table the fork had BEFORE this wave — the legacy shape."""
    tables = [table for name, table in Base.metadata.tables.items() if name not in _NEW_TABLES]
    Base.metadata.create_all(sync_conn, tables=tables)


@pytest.fixture
async def fresh():
    """A brand-new install: full ``create_all``, then the migrations."""
    engine = await create_memory_engine()
    async with engine.begin() as conn:
        await run_migrations(conn)
    yield engine
    await engine.dispose()


@pytest.fixture
async def legacy():
    """An install that predates both tables and the print-log index."""
    import_all_models()
    engine = create_async_engine(MEMORY_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(_create_all_except_the_new_tables)
        # The model declares the index, so create_all just built it; an old install's
        # table predates the declaration, and create_all never revisits an existing table.
        await conn.execute(text(f"DROP INDEX IF EXISTS {_PRINT_LOG_INDEX}"))
    yield engine
    await engine.dispose()


@pytest.fixture
async def upgraded(legacy: AsyncEngine) -> AsyncEngine:
    """The legacy install after a boot — ``create_all`` + ``run_migrations``, one transaction."""
    async with legacy.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await run_migrations(conn)
    return legacy


async def _tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))
    return {row[0] for row in rows}


async def _columns(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(text(f"PRAGMA table_info({table})"))
    return {row[1] for row in rows}


async def _indexes(engine: AsyncEngine, table: str) -> dict[str, str]:
    """Declared indexes on ``table``, name -> its CREATE statement.

    Filtered on a non-null ``sql`` so SQLite's own auto-indexes (which have none) never
    appear as if the model had declared them.
    """
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = :t AND sql IS NOT NULL"),
            {"t": table},
        )
    return {row[0]: row[1] for row in rows}


async def _foreign_keys(engine: AsyncEngine, table: str) -> list[tuple]:
    async with engine.connect() as conn:
        rows = await conn.execute(text(f"PRAGMA foreign_key_list({table})"))
    return list(rows)


async def _insert_span(
    engine: AsyncEngine,
    *,
    printer_id: int,
    started_at: datetime,
    ended_at: datetime | None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            PrinterObservationSpan.__table__.insert().values(
                printer_id=printer_id,
                started_at=started_at,
                last_observed_at=ended_at or started_at,
                ended_at=ended_at,
                is_active=True,
                connected=True,
                gcode_state="RUNNING",
                plate_phase=PLATE_PHASE_CLEAR,
                quarantined=False,
                usb_present=True,
                model_mismatch=False,
            )
        )


class TestFreshDatabase:
    async def test_both_tables_exist(self, fresh):
        assert set(_NEW_TABLES) <= await _tables(fresh)

    async def test_the_span_carries_every_observed_column(self, fresh):
        assert await _columns(fresh, _SPAN_TABLE) == _SPAN_COLUMNS

    async def test_the_episode_carries_every_column(self, fresh):
        assert await _columns(fresh, _EPISODE_TABLE) == _EPISODE_COLUMNS

    async def test_the_span_carries_exactly_its_three_indexes(self, fresh):
        """Three, and no fourth: ``last_observed_at`` is rewritten every tick and must stay unindexed."""
        indexes = await _indexes(fresh, _SPAN_TABLE)
        assert set(indexes) == _SPAN_INDEXES
        assert not [name for name, sql in indexes.items() if "last_observed_at" in sql]

    async def test_the_open_span_index_is_partial_and_unique(self, fresh):
        sql = (await _indexes(fresh, _SPAN_TABLE))[_OPEN_SPAN_INDEX]
        assert "UNIQUE" in sql.upper()
        assert "WHERE ended_at IS NULL" in sql, "without the predicate, a printer could hold only ONE span ever"

    async def test_the_episode_index_leads_with_kind(self, fresh):
        sql = (await _indexes(fresh, _EPISODE_TABLE))[_EPISODE_INDEX]
        assert "kind" in sql and "ended_at" in sql

    async def test_the_print_log_gains_its_created_at_index(self, fresh):
        assert _PRINT_LOG_INDEX in await _indexes(fresh, "print_log_entries")


class TestPreExistingDatabase:
    async def test_the_fixture_really_builds_the_old_shape(self, legacy):
        """Sanity: without this the convergence cases would pass against ``create_all`` alone."""
        assert not set(_NEW_TABLES) & await _tables(legacy)
        assert _PRINT_LOG_INDEX not in await _indexes(legacy, "print_log_entries")

    async def test_it_converges_on_the_same_tables_and_columns(self, upgraded, fresh):
        assert set(_NEW_TABLES) <= await _tables(upgraded)
        assert await _columns(upgraded, _SPAN_TABLE) == await _columns(fresh, _SPAN_TABLE)
        assert await _columns(upgraded, _EPISODE_TABLE) == await _columns(fresh, _EPISODE_TABLE)

    async def test_it_converges_on_the_same_indexes(self, upgraded, fresh):
        assert set(await _indexes(upgraded, _SPAN_TABLE)) == set(await _indexes(fresh, _SPAN_TABLE))
        assert set(await _indexes(upgraded, _EPISODE_TABLE)) == set(await _indexes(fresh, _EPISODE_TABLE))

    async def test_the_print_log_index_is_created_on_an_old_database(self, upgraded):
        assert _PRINT_LOG_INDEX in await _indexes(upgraded, "print_log_entries")

    async def test_a_second_pass_is_a_no_op(self, upgraded):
        """Every boot re-runs the whole list; the index statement must not fail the second time."""
        before = await _indexes(upgraded, "print_log_entries")
        async with upgraded.begin() as conn:
            await run_migrations(conn)
        assert await _indexes(upgraded, "print_log_entries") == before
        assert set(await _indexes(upgraded, _SPAN_TABLE)) == _SPAN_INDEXES


class TestOpenSpanExclusivity:
    async def test_a_second_open_span_for_one_printer_is_refused(self, fresh):
        """The invariant the encoding rests on: two open rows and no reader knows which is current."""
        await _insert_span(fresh, printer_id=1, started_at=BASE_TIME, ended_at=None)
        with pytest.raises(IntegrityError):
            await _insert_span(fresh, printer_id=1, started_at=BASE_TIME + timedelta(minutes=5), ended_at=None)

    async def test_closed_spans_accumulate_freely(self, fresh):
        """History is the product — the predicate excludes closed rows so they never collide."""
        await _insert_span(fresh, printer_id=1, started_at=BASE_TIME, ended_at=BASE_TIME + timedelta(minutes=5))
        await _insert_span(
            fresh,
            printer_id=1,
            started_at=BASE_TIME + timedelta(minutes=5),
            ended_at=BASE_TIME + timedelta(minutes=9),
        )
        async with fresh.connect() as conn:
            count = (await conn.execute(text(f"SELECT COUNT(*) FROM {_SPAN_TABLE} WHERE printer_id = 1"))).scalar()
        assert count == 2

    async def test_an_open_span_per_printer_is_the_normal_state(self, fresh):
        """The index is scoped to the printer: a 12-printer fleet has 12 open spans."""
        await _insert_span(fresh, printer_id=1, started_at=BASE_TIME, ended_at=None)
        await _insert_span(fresh, printer_id=2, started_at=BASE_TIME, ended_at=None)
        async with fresh.connect() as conn:
            open_rows = (
                await conn.execute(text(f"SELECT COUNT(*) FROM {_SPAN_TABLE} WHERE ended_at IS NULL"))
            ).scalar()
        assert open_rows == 2

    async def test_a_closed_span_does_not_block_a_new_open_one(self, fresh):
        """The ordinary tick: close the old tuple, open the next."""
        await _insert_span(fresh, printer_id=1, started_at=BASE_TIME, ended_at=BASE_TIME + timedelta(minutes=5))
        await _insert_span(fresh, printer_id=1, started_at=BASE_TIME + timedelta(minutes=5), ended_at=None)


class TestNoForeignKeys:
    """``printer_id`` is a plain Integer on both tables, on purpose.

    SQLite runs here with FK enforcement OFF, so a declared CASCADE would be inert on
    SQLite and live on PostgreSQL: deleting a printer would erase its history on one
    engine and keep it on the other.
    """

    @pytest.mark.parametrize("table", _NEW_TABLES)
    def test_the_model_declares_none(self, table):
        import_all_models()
        assert Base.metadata.tables[table].foreign_keys == set()

    @pytest.mark.parametrize("table", _NEW_TABLES)
    async def test_the_created_table_has_none(self, fresh, table):
        assert await _foreign_keys(fresh, table) == []


class TestVocabulary:
    """The constants the recorder and the read-time classifier both import."""

    def test_the_plate_phases_fit_their_column(self):
        assert sorted(PLATE_PHASES) == ["clear", "cooling", "ejecting", "held"]
        width = PrinterObservationSpan.__table__.c.plate_phase.type.length
        assert max(len(phase) for phase in PLATE_PHASES) <= width

    def test_the_episode_kinds_fit_their_column(self):
        assert sorted(EPISODE_KINDS) == ["cooldown", "eject"]
        width = FarmCycleEpisode.__table__.c.kind.type.length
        assert max(len(kind) for kind in EPISODE_KINDS) <= width

    def test_the_cooldown_variants_fit_their_column(self):
        assert sorted(COOLDOWN_VARIANTS) == ["fan_only", "hold"]
        width = FarmCycleEpisode.__table__.c.variant.type.length
        assert max(len(variant) for variant in COOLDOWN_VARIANTS) <= width
