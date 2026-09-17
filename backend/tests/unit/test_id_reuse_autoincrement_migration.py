"""Regression test for the AUTOINCREMENT retrofit — a deleted id is never reissued.

005-H2S (2026-09-17). SQLite recycles the rowid of a deleted MAX row unless the table is
declared AUTOINCREMENT, and this fork deliberately leaves FK enforcement off — so a
reference to a purged row does not fail, it silently RE-BINDS to whatever row next takes
that id. On 2026-09-16 ``library_files`` id 109 was purged and re-issued twice; a completed
unit's ``print_queue.library_file_id`` then pointed at a stranger single-plate file, its
eject packed the sweep into plate 1 while the dispatcher commanded plate 3, and the printer
rejected the container as unreadable.

``create_all`` gives a FRESH install the flag from the model, which would mask the
migration entirely — so the fixture strips ``sqlite_autoincrement`` off the eight tables
before ``create_all`` to build the genuine pre-migration shape, seeds every one of them
(including ``print_queue``'s self-referencing retry lineage), adds a hand-written index and
a trigger the model does not declare, and only then runs ``run_migrations``.

The table rebuild is the risky half of this wave: it DROPs and recreates live tables. Every
property that could be quietly lost on the way has its own case below — rows, indexes,
triggers (that they still FIRE, not merely that they exist), the self-FK text, the
``sqlite_sequence`` floor, idempotence, the two pre-flight refusals and the FK-enforcement
guard.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import _AUTOINCREMENT_TABLES, _rebuild_table_with_autoincrement, run_migrations

# The hand-written index and trigger the MODEL does not declare. They stand in for the real
# ones this rebuild has to carry: ``ix_print_queue_dispatch_subtask_id`` and the three
# ``print_archives`` FTS triggers, all created by ``run_migrations`` itself.
_PROBE_INDEX = "ix_ai_probe_archive_filename"
_PROBE_TRIGGER = "ai_probe_archive_insert"
_PROBE_LOG = "ai_probe_log"

# Seeded ids, deliberately non-contiguous so "the max row" is unambiguous per table.
_SEED_MAX_ID = {
    "printers": 2,
    "library_files": 9,
    "print_archives": 7,
    "eject_profiles": 2,
    "skus": 4,
    "sku_files": 6,
    "print_batches": 8,
    "print_queue": 12,
}


@pytest.fixture(autouse=True)
def force_sqlite_dialect(monkeypatch):
    """Force the SQLite branch regardless of test env settings."""
    from backend.app.core import db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    from backend.app.core import database as database_module

    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


def _register_all_models():
    """Import the whole model package so ``create_all`` builds EVERY table.

    Same reasoning as ``test_backup_group_split_column_migration``: ``run_migrations`` is
    one list run top to bottom, and an ``ALTER TABLE`` naming a table a partial import never
    created raises ``no such table``, which ``_safe_execute`` does NOT swallow. A dozen
    model modules are registered by ``init_db``'s own import list and not re-exported from
    the package, so both lists are imported here.
    """
    import backend.app.models  # noqa: F401
    from backend.app.models import (  # noqa: F401
        active_print_spoolman,
        bug_report,
        external_link,
        filament_sku_settings,
        print_log,
        print_queue,
        project_bom,
        shopping_list,
        slot_preset,
        spoolman_k_profile,
        spoolman_slot_assignment,
        virtual_printer,
    )


@contextmanager
def _pre_migration_shape():
    """Strip ``sqlite_autoincrement`` so ``create_all`` builds the OLD schema.

    The flag lives on the shared ``Base.metadata``, so the restore is unconditional — a test
    that left it off would hand every later test in the worker a silently wrong schema.
    """
    from backend.app.core.database import Base

    tables = [Base.metadata.tables[name] for name in _AUTOINCREMENT_TABLES]
    for table in tables:
        table.dialect_options["sqlite"]["autoincrement"] = False
    try:
        yield tables
    finally:
        for table in tables:
            table.dialect_options["sqlite"]["autoincrement"] = True


# The smallest legal row for each table, for the "insert after deleting the max row" probes.
# Seeded through SQLAlchemy Core rather than raw SQL: most NOT NULL columns here carry a
# PYTHON-side default (``position``, ``status``, the whole calibration block …), which only
# the Core insert applies — a raw ``INSERT`` omitting them fails the NOT NULL check.
_MINIMAL_ROW: dict[str, dict] = {
    "printers": {"name": "probe", "serial_number": "SN-probe", "ip_address": "10.0.0.9", "access_code": "ccc"},
    "library_files": {"filename": "p.3mf", "file_path": "/lib/p.3mf", "file_type": "3mf", "file_size": 1},
    "print_archives": {"filename": "p.3mf", "file_path": "/arch/p.3mf", "file_size": 1},
    "eject_profiles": {"name": "probe-profile"},
    "skus": {"code": "SKU-probe", "name": "Probe"},
    "sku_files": {"sku_id": 4, "library_file_id": 5, "plate_index": 9},
    "print_batches": {"name": "probe-run"},
    "print_queue": {"plate_id": 1},
}


async def _insert(conn, table_name: str, **values):
    from backend.app.core.database import Base

    return await conn.execute(Base.metadata.tables[table_name].insert().values(**values))


async def _seed(conn):
    """Seed every one of the eight tables with FK-consistent rows, at explicit ids.

    FK-consistent on purpose: ``PRAGMA foreign_key_check`` must come back empty afterwards,
    which it cannot do if the fixture itself plants dangling references.
    """
    await _insert(
        conn, "printers", id=1, name="unit-001", serial_number="SN001", ip_address="10.0.0.1", access_code="aaa"
    )
    await _insert(
        conn, "printers", id=2, name="unit-002", serial_number="SN002", ip_address="10.0.0.2", access_code="bbb"
    )
    await _insert(
        conn,
        "library_files",
        id=5,
        filename="half-shell.gcode.3mf",
        file_path="/lib/half-shell.gcode.3mf",
        file_type="gcode.3mf",
        file_size=1024,
    )
    await _insert(
        conn,
        "library_files",
        id=9,
        filename="drill-driver.gcode.3mf",
        file_path="/lib/drill-driver.gcode.3mf",
        file_type="gcode.3mf",
        file_size=2048,
    )
    await _insert(
        conn,
        "print_archives",
        id=3,
        printer_id=1,
        filename="zebrafish.gcode.3mf",
        file_path="/arch/zebrafish.gcode.3mf",
        file_size=4096,
    )
    await _insert(
        conn,
        "print_archives",
        id=7,
        printer_id=2,
        filename="gudgeon.gcode.3mf",
        file_path="/arch/gudgeon.gcode.3mf",
        file_size=8192,
    )
    await _insert(conn, "eject_profiles", id=1, name="petg-textured")
    await _insert(conn, "eject_profiles", id=2, name="pla-smooth")
    await _insert(conn, "skus", id=4, code="SKU007.01", name="Half Shell")
    await _insert(conn, "sku_files", id=6, sku_id=4, library_file_id=5, plate_index=3, units_per_plate=1)
    await _insert(conn, "print_batches", id=8, name="run-285", sku_file_id=6)
    # 11 is the original attempt, 12 its retry — the self-FK the rebuild must not rewrite.
    common = {"printer_id": 1, "archive_id": 3, "library_file_id": 5, "batch_id": 8, "plate_id": 3}
    await _insert(conn, "print_queue", id=11, retry_of_id=None, **common)
    await _insert(conn, "print_queue", id=12, retry_of_id=11, **common)

    await conn.execute(text(f"CREATE TABLE {_PROBE_LOG} (note TEXT)"))
    await conn.execute(text(f"CREATE INDEX {_PROBE_INDEX} ON print_archives (filename)"))
    await conn.execute(
        text(
            f"CREATE TRIGGER {_PROBE_TRIGGER} AFTER INSERT ON print_archives BEGIN "
            f"INSERT INTO {_PROBE_LOG} (note) VALUES ('fired-' || new.id); END"
        )
    )


async def _build_pre_migration_engine(*, enforce_foreign_keys: bool = False):
    from backend.app.core.database import Base

    _register_all_models()
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    if enforce_foreign_keys:

        @event.listens_for(eng.sync_engine, "connect")
        def _enable_fks(dbapi_conn, _record):  # pragma: no cover - trivial
            # Must be set on the raw connection: SQLite ignores the pragma inside a transaction.
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.close()

    async with eng.begin() as conn:
        with _pre_migration_shape():
            await conn.run_sync(Base.metadata.create_all)
        await _seed(conn)
    return eng


@pytest.fixture
async def engine():
    eng = await _build_pre_migration_engine()
    yield eng
    await eng.dispose()


@pytest.fixture
async def migrated(engine):
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        # Fixture artifact, not a property of the migration: the seeds are inserted BEFORE
        # run_migrations creates the FTS triggers, so they are absent from the external-content
        # index and FTS5 answers "database disk image is malformed" when one is later deleted.
        # A real install's archives are indexed on the way in. (The rebuild itself keeps table
        # and index in step — DROP TABLE fires no delete trigger and the staging INSERT fires
        # no insert trigger, so the rowids stay exactly as they were.)
        await conn.execute(text("INSERT INTO archive_fts(archive_fts) VALUES('rebuild')"))
    return engine


async def _table_sql(conn, table: str) -> str:
    return (
        await conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table})
    ).scalar()


async def _objects(conn, table: str) -> set[str]:
    rows = await conn.execute(
        text(
            "SELECT name FROM sqlite_master WHERE tbl_name = :t AND type IN ('index', 'trigger') "
            "AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        ),
        {"t": table},
    )
    return {row[0] for row in rows}


async def _counts(conn) -> dict[str, int]:
    out = {}
    for table in _AUTOINCREMENT_TABLES:
        out[table] = (await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar()
    return out


class TestPreMigrationShape:
    async def test_fixture_really_builds_the_old_schema(self, engine):
        """Sanity: without this the whole file would pass against ``create_all``'s own flag."""
        async with engine.connect() as conn:
            for table in _AUTOINCREMENT_TABLES:
                assert "AUTOINCREMENT" not in (await _table_sql(conn, table)).upper(), table

    def test_the_strip_restores_the_flag_on_the_shared_metadata(self):
        """The strip mutates module-global metadata — a leak would mis-build every later test."""
        from backend.app.core.database import Base

        _register_all_models()
        with _pre_migration_shape() as tables:
            assert all(t.dialect_options["sqlite"]["autoincrement"] is False for t in tables)
        for name in _AUTOINCREMENT_TABLES:
            assert Base.metadata.tables[name].dialect_options["sqlite"]["autoincrement"] is True, name


class TestRebuild:
    async def test_all_eight_tables_carry_autoincrement(self, migrated):
        """(a) The whole point: every operator-deletable table stops recycling ids."""
        async with migrated.connect() as conn:
            for table in _AUTOINCREMENT_TABLES:
                assert "AUTOINCREMENT" in (await _table_sql(conn, table)).upper(), table

    async def test_no_row_is_lost(self, engine):
        """(b) Row counts before and after are the same table by table."""
        async with engine.connect() as conn:
            before = await _counts(conn)
        async with engine.begin() as conn:
            await run_migrations(conn)
        async with engine.connect() as conn:
            after = await _counts(conn)
        assert after == before
        assert all(count > 0 for count in before.values()), "the fixture must seed every table"

    async def test_hand_written_index_and_trigger_survive_by_name(self, migrated):
        """(b) ``DROP TABLE`` takes indexes and triggers with it; they are replayed verbatim."""
        async with migrated.connect() as conn:
            objects = await _objects(conn, "print_archives")
        assert _PROBE_INDEX in objects
        assert _PROBE_TRIGGER in objects
        # The three FTS triggers run_migrations creates are the real-world case.
        assert {"archive_fts_insert", "archive_fts_delete", "archive_fts_update"} <= objects

    async def test_the_replayed_trigger_still_fires(self, migrated):
        """(b) Existing by name is not the property that matters — firing is."""
        async with migrated.begin() as conn:
            await _insert(
                conn,
                "print_archives",
                id=101,
                filename="post-rebuild.3mf",
                file_path="/arch/post-rebuild.3mf",
                file_size=16,
            )
        async with migrated.connect() as conn:
            notes = (await conn.execute(text(f"SELECT note FROM {_PROBE_LOG}"))).scalars().all()
        assert notes == ["fired-101"]

    async def test_the_fts_triggers_still_index_new_archives(self, migrated):
        """(b) The FTS triggers are external-content: a broken replay loses archive search."""
        async with migrated.begin() as conn:
            await _insert(
                conn,
                "print_archives",
                id=102,
                filename="q.3mf",
                file_path="/arch/q.3mf",
                file_size=16,
                print_name="quokka",
            )
        async with migrated.connect() as conn:
            hits = (
                (await conn.execute(text("SELECT rowid FROM archive_fts WHERE archive_fts MATCH 'quokka'")))
                .scalars()
                .all()
            )
        assert hits == [102]

    async def test_deleting_the_max_row_does_not_free_its_id(self, migrated):
        """(c) THE incident. Purge the top library file, upload another: it must not be 9."""
        async with migrated.begin() as conn:
            await conn.execute(text("DELETE FROM library_files WHERE id = 9"))
            await _insert(
                conn,
                "library_files",
                filename="m12-drill-driver.gcode.3mf",
                file_path="/lib/m12.gcode.3mf",
                file_type="gcode.3mf",
                file_size=512,
            )
        async with migrated.connect() as conn:
            new_id = (await conn.execute(text("SELECT MAX(id) FROM library_files"))).scalar()
        assert new_id > _SEED_MAX_ID["library_files"], (
            "SQLite reissued the purged id — sku_files/print_queue rows still naming 9 would now "
            "point at a stranger file, which is exactly the 005-H2S failure"
        )

    async def test_every_table_refuses_to_reissue_its_deleted_max_id(self, migrated):
        """(c) The property is claimed for all eight, so it is checked on all eight."""
        # Child-first, so each delete leaves the remaining seeds FK-consistent for as long
        # as it can; the later parents' deletes deliberately leave the usual dangling
        # references behind, because that is what this fork does and what must stay safe.
        for table in reversed(_AUTOINCREMENT_TABLES):
            max_id = _SEED_MAX_ID[table]
            async with migrated.begin() as conn:
                await conn.execute(text(f"DELETE FROM {table} WHERE id = :i"), {"i": max_id})
                await _insert(conn, table, **_MINIMAL_ROW[table])
                fresh = (await conn.execute(text(f"SELECT MAX(id) FROM {table}"))).scalar()
            assert fresh > max_id, f"{table} reissued id {max_id}"

    async def test_sqlite_sequence_is_seeded_at_least_to_max_id(self, migrated):
        """(f) The helper never writes ``sqlite_sequence``; the explicit-rowid INSERT seeds it."""
        async with migrated.connect() as conn:
            for table in _AUTOINCREMENT_TABLES:
                seq = (
                    await conn.execute(text("SELECT seq FROM sqlite_sequence WHERE name = :t"), {"t": table})
                ).scalar()
                max_id = (await conn.execute(text(f"SELECT MAX(id) FROM {table}"))).scalar()
                assert seq is not None, f"{table} has no sqlite_sequence row"
                assert seq >= max_id, f"{table}: seq {seq} below max(id) {max_id}"

    async def test_foreign_key_check_is_clean(self, migrated):
        """(h) DROP + RENAME must leave every reference resolvable on FK-consistent data."""
        async with migrated.connect() as conn:
            violations = (await conn.execute(text("PRAGMA foreign_key_check"))).fetchall()
        assert violations == []

    async def test_the_print_queue_self_fk_still_names_print_queue(self, migrated):
        """Only the leading CREATE TABLE token is renamed — never a ``REFERENCES`` clause.

        ``print_queue.retry_of_id`` points at ``print_queue`` itself. Renaming the reference
        along with the table would leave the retry lineage pointing at ``print_queue__ai``,
        a table that no longer exists.
        """
        async with migrated.connect() as conn:
            ddl = await _table_sql(conn, "print_queue")
            lineage = (await conn.execute(text("SELECT retry_of_id FROM print_queue WHERE id = 12"))).scalar()
        assert "__ai" not in ddl
        assert "REFERENCES print_queue" in ddl
        assert lineage == 11

    async def test_no_staging_table_is_left_behind(self, migrated):
        async with migrated.connect() as conn:
            leftovers = (
                (await conn.execute(text("SELECT name FROM sqlite_master WHERE name LIKE '%\\_\\_ai' ESCAPE '\\'")))
                .scalars()
                .all()
            )
        assert leftovers == []

    async def test_second_run_is_a_no_op(self, migrated, caplog):
        """(d) Every boot re-runs the migration list; the rebuild must not repeat."""
        async with migrated.connect() as conn:
            before = await _counts(conn)
        with caplog.at_level(logging.INFO, logger="backend.app.core.database"):
            async with migrated.begin() as conn:
                await run_migrations(conn)
        async with migrated.connect() as conn:
            after = await _counts(conn)
            for table in _AUTOINCREMENT_TABLES:
                assert "AUTOINCREMENT" in (await _table_sql(conn, table)).upper(), table
        assert after == before
        assert not [r for r in caplog.records if "AUTOINCREMENT rebuild" in r.getMessage()], (
            "the second pass rebuilt a table that already carried the flag"
        )


class TestPreFlightRefusals:
    async def test_a_table_with_an_unknown_live_column_is_skipped_intact(self, engine, caplog):
        """(e) A live column the model lacks would be dropped by the copy — so we do not copy.

        The skip is an ERROR log and not an exception on purpose: one un-retrofitted table
        must not take an install's startup down, and the other seven still get the fix.
        """
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE skus ADD COLUMN legacy_note TEXT"))
            await conn.execute(text("UPDATE skus SET legacy_note = 'keep me' WHERE id = 4"))

        with caplog.at_level(logging.ERROR, logger="backend.app.core.database"):
            async with engine.begin() as conn:
                await run_migrations(conn)

        async with engine.connect() as conn:
            skus_sql = await _table_sql(conn, "skus")
            kept = (await conn.execute(text("SELECT legacy_note FROM skus WHERE id = 4"))).scalar()
            others = {t: (await _table_sql(conn, t)).upper() for t in _AUTOINCREMENT_TABLES if t != "skus"}

        assert "AUTOINCREMENT" not in skus_sql.upper(), "skus must be left exactly as it was"
        assert kept == "keep me", "the unknown column's data must survive untouched"
        assert all("AUTOINCREMENT" in sql for sql in others.values()), "the other seven are still rebuilt"
        skipped = [r for r in caplog.records if "SKIPPED for skus" in r.getMessage()]
        assert len(skipped) == 1 and skipped[0].levelno == logging.ERROR
        assert "legacy_note" in skipped[0].getMessage()

    async def test_a_missing_not_null_column_with_no_default_is_skipped_intact(self, engine, caplog):
        """(e, second arm) The copy would have nothing to put in the column, so it does not run.

        The helper is called directly rather than through ``run_migrations``: dropping
        ``printers.access_code`` also breaks the unrelated virtual-printer access-code sync
        migration, which would abort the pass long before the rebuild is reached. The
        pre-flight refusal is this helper's property, so it is exercised on this helper.
        """
        from backend.app.core.database import Base

        async with engine.begin() as conn:
            # printers.access_code is NOT NULL in the model with neither a server nor a Python default.
            await conn.execute(text("ALTER TABLE printers DROP COLUMN access_code"))

        with caplog.at_level(logging.ERROR, logger="backend.app.core.database"):
            async with engine.begin() as conn:
                await _rebuild_table_with_autoincrement(conn, Base.metadata.tables["printers"])

        async with engine.connect() as conn:
            printers_sql = await _table_sql(conn, "printers")
            rows = (await conn.execute(text("SELECT COUNT(*) FROM printers"))).scalar()
        assert "AUTOINCREMENT" not in printers_sql.upper()
        assert rows == 2
        skipped = [r for r in caplog.records if "SKIPPED for printers" in r.getMessage()]
        assert len(skipped) == 1 and "access_code" in skipped[0].getMessage()
        assert skipped[0].levelno == logging.ERROR

    async def test_it_refuses_to_run_while_foreign_keys_are_enforced(self, caplog):
        """(g) A DROP under enforced FKs cascade-deletes children. Raise, and never DROP.

        The pragma is deliberately not turned off by the helper: an install that enabled it
        made a decision this migration must not silently reverse.
        """
        from backend.app.core.database import Base

        eng = await _build_pre_migration_engine(enforce_foreign_keys=True)
        try:
            async with eng.connect() as conn:
                assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 1, "fixture precondition"

            with pytest.raises(RuntimeError, match="foreign_keys"):
                async with eng.begin() as conn:
                    await _rebuild_table_with_autoincrement(conn, Base.metadata.tables["print_queue"])

            async with eng.connect() as conn:
                ddl = await _table_sql(conn, "print_queue")
                rows = (await conn.execute(text("SELECT COUNT(*) FROM print_queue"))).scalar()
                children = (await conn.execute(text("SELECT COUNT(*) FROM print_archives"))).scalar()
            assert ddl is not None and "AUTOINCREMENT" not in ddl.upper()
            assert rows == 2, "the table must be untouched — the refusal happens before any DROP"
            assert children == 2
        finally:
            await eng.dispose()

    async def test_a_table_absent_from_the_database_is_a_no_op(self, engine):
        """Fresh-install ordering: the helper is asked about a table that is not there yet."""
        from backend.app.core.database import Base

        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE sku_files"))
            await _rebuild_table_with_autoincrement(conn, Base.metadata.tables["sku_files"])
            still_gone = (
                await conn.execute(text("SELECT COUNT(*) FROM sqlite_master WHERE name = 'sku_files'"))
            ).scalar()
        assert still_gone == 0, "the helper must not resurrect a table it was merely asked about"
