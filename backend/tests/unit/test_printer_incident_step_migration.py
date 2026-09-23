"""``printer_incident_step`` — the recovery driver's step ledger (2026-09-23, 012-H2S).

The wire cannot restate which verbs a driver already sent: a stalled feeder answers every
release lever with the same re-PAUSE in the same change, so a driver that re-entered after
a restart started its ladder over and re-ground a feeder it had already proved stalled.
The ledger is how re-entry resumes at the next unpulled lever, so the table has to exist
on every install, and both ways of building it must agree:

* a FRESH install builds it from the model — ``create_all`` is what ``init_db`` runs first;
* an install that predates it builds it from ``run_migrations``' idempotent DDL;
* both converge on ONE object per index name (compared as the COMPLETE index set, so a
  redundant anonymous unique index cannot hide), and ``run_migrations`` runs on every
  boot, so a second pass changes nothing and loses no row.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.app.core.database import run_migrations
from backend.app.models.printer_incident_step import PrinterIncidentStep
from backend.tests._fixtures.db import boot_schema, create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")

_TABLE = "printer_incident_step"

# index name -> (unique, columns in order). The whole set: nothing else may exist.
_EXPECTED_INDEXES: dict[str, tuple[bool, list[str]]] = {
    "ux_printer_incident_step_seq": (True, ["incident_id", "seq"]),
    "ix_printer_incident_step_incident": (False, ["incident_id"]),
}

_INSERT_STEP = text(
    f"INSERT INTO {_TABLE} (incident_id, seq, kind, name, sent_at) "
    "VALUES (:incident_id, :seq, 'lever', 'resume', '2026-09-23 02:01:43')"
)


@pytest.fixture
async def engine():
    eng = await create_memory_engine()
    yield eng
    await eng.dispose()


async def _drop_table(engine) -> None:
    """The pre-wave schema: everything the model set builds, minus this table."""
    async with engine.begin() as conn:
        await conn.execute(text(f"DROP TABLE {_TABLE}"))
        assert not await _table_exists(conn)


async def _migrate(engine) -> None:
    async with engine.begin() as conn:
        await run_migrations(conn)


async def _table_exists(conn) -> bool:
    row = await conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"), {"name": _TABLE}
    )
    return row.scalar() is not None


async def _indexes(conn) -> dict[str, tuple[bool, list[str]]]:
    """EVERY index SQLite holds on the table, anonymous autoindexes included."""
    found: dict[str, tuple[bool, list[str]]] = {}
    for row in (await conn.execute(text(f"PRAGMA index_list('{_TABLE}')"))).all():
        name, unique = row[1], bool(row[2])
        columns = [info[2] for info in (await conn.execute(text(f"PRAGMA index_info('{name}')"))).all()]
        found[name] = (unique, columns)
    return found


async def _columns(conn) -> dict[str, bool]:
    """column name -> NOT NULL, for every non-key column (SQLite reports a rowid-alias
    primary key's NOT NULL differently depending on how the DDL spelled it)."""
    rows = (await conn.execute(text(f"PRAGMA table_info('{_TABLE}')"))).all()
    return {row[1]: bool(row[3]) for row in rows if not row[5]}


_MODEL_COLUMNS: dict[str, bool] = {
    column.name: not column.nullable for column in PrinterIncidentStep.__table__.columns if not column.primary_key
}


async def _schema(engine) -> tuple[dict[str, tuple[bool, list[str]]], dict[str, bool]]:
    async with engine.connect() as conn:
        assert await _table_exists(conn)
        return await _indexes(conn), await _columns(conn)


@pytest.mark.asyncio
async def test_a_fresh_install_builds_the_table_from_the_model(engine):
    """``create_all`` alone (what ``init_db`` runs first): the table and exactly the two
    named indexes. An exact set, so a UniqueConstraint's anonymous autoindex would fail it."""
    indexes, columns = await _schema(engine)

    assert indexes == _EXPECTED_INDEXES
    assert columns == _MODEL_COLUMNS


@pytest.mark.asyncio
async def test_a_fresh_boot_converges_on_one_object_per_index(engine):
    """The real boot is ``create_all`` THEN ``run_migrations`` over it: the DDL's
    IF NOT EXISTS must meet the model's own index names and add nothing beside them."""
    await boot_schema(engine)

    indexes, columns = await _schema(engine)

    assert indexes == _EXPECTED_INDEXES
    assert columns == _MODEL_COLUMNS


@pytest.mark.asyncio
async def test_run_migrations_builds_the_table_on_an_install_without_it(engine):
    await _drop_table(engine)

    await _migrate(engine)

    indexes, columns = await _schema(engine)
    assert indexes == _EXPECTED_INDEXES
    assert columns == _MODEL_COLUMNS


@pytest.mark.asyncio
async def test_the_migrated_foreign_key_cascades_with_the_incident(engine):
    """The DDL's own REFERENCES clause, read back: the step rows belong to the incident."""
    await _drop_table(engine)
    await _migrate(engine)

    async with engine.connect() as conn:
        keys = (await conn.execute(text(f"PRAGMA foreign_key_list('{_TABLE}')"))).all()

    # (table, from, to, on_delete) — PRAGMA foreign_key_list columns 2, 3, 4, 6.
    assert [(key[2], key[3], key[4], key[6]) for key in keys] == [("printer_incident", "incident_id", "id", "CASCADE")]


@pytest.mark.asyncio
async def test_the_migrated_seq_index_refuses_a_second_step_with_the_same_seq(engine):
    """One step per (incident, seq), enforced by the migrated index itself. No incident
    row is needed: this fork opens SQLite with foreign keys OFF, and what is under test
    here is the UNIQUE index, not referential integrity."""
    await _drop_table(engine)
    await _migrate(engine)

    async with engine.begin() as conn:
        await conn.execute(_INSERT_STEP, {"incident_id": 1, "seq": 1})
        await conn.execute(_INSERT_STEP, {"incident_id": 1, "seq": 2})
        await conn.execute(_INSERT_STEP, {"incident_id": 2, "seq": 1})

    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(_INSERT_STEP, {"incident_id": 1, "seq": 1})


@pytest.mark.parametrize("origin", ["create_all", "run_migrations"])
@pytest.mark.asyncio
async def test_a_second_boot_changes_nothing_and_keeps_every_row(engine, origin):
    """``run_migrations`` runs on EVERY boot, over either origin of the table."""
    if origin == "run_migrations":
        await _drop_table(engine)
    await _migrate(engine)
    async with engine.begin() as conn:
        await conn.execute(_INSERT_STEP, {"incident_id": 1, "seq": 1})
    before = await _schema(engine)

    await _migrate(engine)

    assert await _schema(engine) == before == (_EXPECTED_INDEXES, _MODEL_COLUMNS)
    async with engine.connect() as conn:
        assert (await conn.execute(text(f"SELECT COUNT(*) FROM {_TABLE}"))).scalar() == 1
