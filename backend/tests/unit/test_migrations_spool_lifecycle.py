"""Regression tests for the spool-lifecycle migration (WI-1 / WI-5).

Two migrations, both appended to ``run_migrations``:

WI-1 (FIFO substrate): add ``spool.first_loaded_at`` and backfill it with
``created_at`` for any spool that has ever been in service (has an assignment,
usage history, a ``last_used`` timestamp, or consumed grams). Pristine,
never-assigned inventory spools stay NULL.

WI-5 (settings remap): the boolean ``prefer_lowest_filament`` setting is
replaced by the tri-state ``spool_selection_policy``. A truthy legacy flag maps
to ``spool_selection_policy = 'lowest_remaining'``; false/absent maps to
nothing (the new ``first_loaded`` default applies); the old key is always
dropped. Both migrations are idempotent and SQLite-safe (mirrors the other
migration regression tests in this suite).
"""

from __future__ import annotations

import logging
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.database import run_migrations

_FIXED_CREATED = datetime(2026, 1, 1, 12, 0, 0)


@pytest.fixture(autouse=True)
def force_sqlite_dialect(monkeypatch):
    """Force the SQLite branch regardless of test env settings."""
    from backend.app.core import db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    from backend.app.core import database as database_module

    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


def _register_all_models():
    from backend.app.models import (  # noqa: F401
        ams_history,
        ams_label,
        api_key,
        archive,
        color_catalog,
        eject_profile,
        external_link,
        filament,
        group,
        kprofile_note,
        library,
        maintenance,
        notification,
        notification_template,
        print_log,
        print_queue,
        printer,
        project,
        project_bom,
        settings,
        slot_preset,
        smart_plug,
        smart_plug_energy_snapshot,
        spool,
        spool_assignment,
        spool_catalog,
        spool_k_profile,
        spool_usage_history,
        spoolbuddy_device,
        user,
        user_email_pref,
        virtual_printer,
    )


@pytest.fixture
async def engine():
    from backend.app.core.database import Base

    _register_all_models()

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def session_maker(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _spool_columns(conn) -> set[str]:
    rows = (await conn.execute(text("PRAGMA table_info(spool)"))).fetchall()
    return {row[1] for row in rows}


async def _make_spool(session: AsyncSession, material: str = "PETG", **kwargs):
    from backend.app.models.spool import Spool

    spool = Spool(material=material, created_at=_FIXED_CREATED, **kwargs)
    session.add(spool)
    await session.flush()
    return spool


# ---------------------------------------------------------------------------
# WI-1: first_loaded_at column + backfill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_readds_first_loaded_at_column(engine):
    """Dropping the column simulates a pre-migration schema; run_migrations re-adds it."""
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE spool DROP COLUMN first_loaded_at"))
        assert "first_loaded_at" not in await _spool_columns(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        assert "first_loaded_at" in await _spool_columns(conn)


@pytest.mark.asyncio
async def test_backfill_stamps_in_service_spools_only(engine, session_maker):
    """Assigned / used / last_used / consumed spools get first_loaded_at=created_at;
    a pristine never-assigned spool stays NULL."""
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.models.spool_usage_history import SpoolUsageHistory

    async with session_maker() as session:
        assigned = await _make_spool(session)
        used = await _make_spool(session)
        consumed = await _make_spool(session, weight_used=5.0)
        recently_used = await _make_spool(session, last_used=datetime(2026, 2, 2, 9, 0, 0))
        pristine = await _make_spool(session)

        session.add(SpoolAssignment(spool_id=assigned.id, printer_id=1, ams_id=0, tray_id=0))
        session.add(SpoolUsageHistory(spool_id=used.id, weight_used=10.0, percent_used=1))
        await session.commit()

        ids = {
            "assigned": assigned.id,
            "used": used.id,
            "consumed": consumed.id,
            "recently_used": recently_used.id,
            "pristine": pristine.id,
        }

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT id, first_loaded_at, created_at FROM spool"))).fetchall()
    by_id = {r[0]: (r[1], r[2]) for r in rows}

    for label in ("assigned", "used", "consumed", "recently_used"):
        first_loaded, created = by_id[ids[label]]
        assert first_loaded is not None, f"{label} should be backfilled"
        assert first_loaded == created, f"{label} first_loaded_at should equal created_at"

    assert by_id[ids["pristine"]][0] is None, "pristine unassigned spool must stay NULL"


@pytest.mark.asyncio
async def test_backfill_is_idempotent(engine, session_maker):
    """Running the migration twice never re-stamps or clobbers; pristine stays NULL."""
    from backend.app.models.spool_assignment import SpoolAssignment

    async with session_maker() as session:
        assigned = await _make_spool(session)
        pristine = await _make_spool(session)
        session.add(SpoolAssignment(spool_id=assigned.id, printer_id=1, ams_id=0, tray_id=0))
        await session.commit()
        assigned_id, pristine_id = assigned.id, pristine.id

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        first_pass = {
            r[0]: r[1] for r in (await conn.execute(text("SELECT id, first_loaded_at FROM spool"))).fetchall()
        }

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        second_pass = {
            r[0]: r[1] for r in (await conn.execute(text("SELECT id, first_loaded_at FROM spool"))).fetchall()
        }

    assert first_pass == second_pass
    assert first_pass[assigned_id] is not None
    assert first_pass[pristine_id] is None


# ---------------------------------------------------------------------------
# WI-5: prefer_lowest_filament -> spool_selection_policy remap
# ---------------------------------------------------------------------------


async def _seed_settings(session: AsyncSession, **kv):
    from backend.app.models.settings import Settings

    for key, value in kv.items():
        session.add(Settings(key=key, value=value))
    await session.commit()


async def _settings_map(conn) -> dict[str, str]:
    rows = (await conn.execute(text("SELECT key, value FROM settings"))).fetchall()
    return {r[0]: r[1] for r in rows}


@pytest.mark.asyncio
async def test_remap_true_becomes_lowest_remaining(engine, session_maker):
    async with session_maker() as session:
        await _seed_settings(session, prefer_lowest_filament="true")

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert settings.get("spool_selection_policy") == "lowest_remaining"
    assert "prefer_lowest_filament" not in settings


@pytest.mark.asyncio
async def test_remap_true_case_insensitive(engine, session_maker):
    """Bool settings are stored 'true' but a capitalized 'True' must still remap."""
    async with session_maker() as session:
        await _seed_settings(session, prefer_lowest_filament="True")

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert settings.get("spool_selection_policy") == "lowest_remaining"
    assert "prefer_lowest_filament" not in settings


@pytest.mark.asyncio
async def test_remap_false_creates_no_policy(engine, session_maker):
    async with session_maker() as session:
        await _seed_settings(session, prefer_lowest_filament="false")

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert "spool_selection_policy" not in settings
    assert "prefer_lowest_filament" not in settings


@pytest.mark.asyncio
async def test_remap_absent_creates_no_policy(engine):
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert "spool_selection_policy" not in settings
    assert "prefer_lowest_filament" not in settings


@pytest.mark.asyncio
async def test_remap_preserves_existing_policy(engine, session_maker):
    """If a policy row already exists, the truthy legacy flag must NOT overwrite it."""
    async with session_maker() as session:
        await _seed_settings(session, prefer_lowest_filament="true", spool_selection_policy="slot_order")

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert settings.get("spool_selection_policy") == "slot_order"
    assert "prefer_lowest_filament" not in settings


@pytest.mark.asyncio
async def test_remap_is_idempotent(engine, session_maker):
    async with session_maker() as session:
        await _seed_settings(session, prefer_lowest_filament="true")

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert settings.get("spool_selection_policy") == "lowest_remaining"
    assert "prefer_lowest_filament" not in settings


# ---------------------------------------------------------------------------
# FIFO seating-order fix: loaded_at column + backfill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_readds_loaded_at_column(engine):
    """Dropping the column simulates a pre-migration schema; run_migrations re-adds it
    (idempotent ADD COLUMN)."""
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE spool DROP COLUMN loaded_at"))
        assert "loaded_at" not in await _spool_columns(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        assert "loaded_at" in await _spool_columns(conn)
    # Re-add is idempotent — a second run does not raise on the existing column.
    async with engine.begin() as conn:
        await run_migrations(conn)


@pytest.mark.asyncio
async def test_loaded_at_backfill_coalesce_order(engine, session_maker):
    """Backfill = COALESCE(first_loaded_at, created_at) for in-service rows; a pristine
    never-assigned spool stays NULL. A distinct first_loaded_at proves COALESCE prefers
    it over created_at (not just that both happen to equal created_at)."""
    from backend.app.models.spool_assignment import SpoolAssignment

    distinct_first = datetime(2026, 6, 15, 8, 0, 0)  # != _FIXED_CREATED
    async with session_maker() as session:
        # first_loaded_at pre-set and distinct → loaded_at must copy IT, not created_at.
        with_first = await _make_spool(session, first_loaded_at=distinct_first)
        # bound, no first_loaded_at: WI-1 stamps first_loaded_at=created_at, so loaded_at
        # resolves to created_at (the in-service COALESCE-fallback value).
        bound = await _make_spool(session)
        pristine = await _make_spool(session)
        session.add(SpoolAssignment(spool_id=bound.id, printer_id=1, ams_id=0, tray_id=0))
        await session.commit()
        ids = {"with_first": with_first.id, "bound": bound.id, "pristine": pristine.id}

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT id, loaded_at, first_loaded_at, created_at FROM spool"))).fetchall()
    by_id = {r[0]: (r[1], r[2], r[3]) for r in rows}

    loaded, first, _created = by_id[ids["with_first"]]
    assert loaded is not None and loaded == first, "loaded_at should copy the distinct first_loaded_at"

    loaded, first, created = by_id[ids["bound"]]
    assert loaded is not None and loaded == created == first, "bound row → loaded_at = created_at"

    assert by_id[ids["pristine"]][0] is None, "pristine unassigned spool must stay NULL"


@pytest.mark.asyncio
async def test_loaded_at_backfill_never_overwrites_and_is_idempotent(engine, session_maker):
    """An existing loaded_at stamp is never clobbered (even when first_loaded_at differs),
    and running the migration twice is a no-op."""
    prior_loaded = datetime(2026, 7, 20, 10, 0, 0)
    older_first = datetime(2026, 1, 1, 12, 0, 0)
    async with session_maker() as session:
        stamped = await _make_spool(session, loaded_at=prior_loaded, first_loaded_at=older_first)
        pristine = await _make_spool(session)
        await session.commit()
        stamped_id, pristine_id = stamped.id, pristine.id

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        first_pass = {r[0]: r[1] for r in (await conn.execute(text("SELECT id, loaded_at FROM spool"))).fetchall()}

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        second_pass = {r[0]: r[1] for r in (await conn.execute(text("SELECT id, loaded_at FROM spool"))).fetchall()}

    assert first_pass == second_pass, "second run must not change any loaded_at"
    # The pre-existing stamp survived (not overwritten to first_loaded_at).
    assert first_pass[stamped_id] is not None and first_pass[stamped_id] != str(older_first)
    assert first_pass[pristine_id] is None


# ---------------------------------------------------------------------------
# R1: min_start_spool_g default 120 -> 150 migration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_min_start_old_default_migrated(engine, session_maker):
    """A stored value still on the OLD default (120) is bumped to the new default 150."""
    async with session_maker() as session:
        await _seed_settings(session, min_start_spool_g="120")

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert settings.get("min_start_spool_g") == "150"


@pytest.mark.asyncio
async def test_min_start_custom_value_untouched(engine, session_maker):
    """A deliberately-different operator value (not the old default) is left alone."""
    async with session_maker() as session:
        await _seed_settings(session, min_start_spool_g="80")

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert settings.get("min_start_spool_g") == "80"


@pytest.mark.asyncio
async def test_min_start_absent_stays_absent(engine):
    """No stored row → the migration creates nothing (schema default 150 materialises at
    read time)."""
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert "min_start_spool_g" not in settings


@pytest.mark.asyncio
async def test_min_start_migration_idempotent(engine, session_maker):
    """Running twice keeps 150 (the second pass no longer matches value='120')."""
    async with session_maker() as session:
        await _seed_settings(session, min_start_spool_g="120")

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        settings = await _settings_map(conn)
    assert settings.get("min_start_spool_g") == "150"


# ---------------------------------------------------------------------------
# 012-H2S: duplicate spool bindings deduped, then ux_spool_assignment_spool_id
# ---------------------------------------------------------------------------

_DB_LOGGER = "backend.app.core.database"
_DUP_WARNING = "dropping stale duplicate spool binding"

# The table exactly as it was BEFORE the structural invariant landed: the SLOT is
# unique, ``spool_id`` is not — which is how one roll came to be bound to two trays.
# ``create_all`` builds the post-fix table from the model, so the pre-migration shape
# has to be re-created by hand (SQLite cannot drop a column constraint).
_PRE_UNIQUE_SPOOL_ASSIGNMENT_DDL = """
CREATE TABLE spool_assignment (
    id INTEGER NOT NULL,
    spool_id INTEGER NOT NULL,
    printer_id INTEGER NOT NULL,
    ams_id INTEGER NOT NULL,
    tray_id INTEGER NOT NULL,
    fingerprint_color VARCHAR(8),
    fingerprint_type VARCHAR(50),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (printer_id, ams_id, tray_id),
    FOREIGN KEY(spool_id) REFERENCES spool (id) ON DELETE CASCADE,
    FOREIGN KEY(printer_id) REFERENCES printers (id) ON DELETE CASCADE
)
"""


async def _downgrade_spool_assignment(engine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE spool_assignment"))
        await conn.execute(text(_PRE_UNIQUE_SPOOL_ASSIGNMENT_DDL))


async def _binding_rows(conn) -> list[tuple]:
    rows = (
        await conn.execute(text("SELECT id, spool_id, printer_id, ams_id, tray_id FROM spool_assignment ORDER BY id"))
    ).fetchall()
    return [tuple(r) for r in rows]


async def _has_unique_spool_index(conn) -> bool:
    row = (
        await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'ux_spool_assignment_spool_id'")
        )
    ).fetchone()
    return row is not None


async def _make_printer(session, printer_id: int = 1):
    """A real ``printers`` row. Bindings must point at one: the orphan purge (W2)
    deletes assignments whose printer no longer exists, so a fixture that relied on
    SQLite's unenforced FK would be swept away by the very migration under test."""
    from backend.app.models.printer import Printer

    printer = Printer(
        id=printer_id,
        name=f"P{printer_id}",
        serial_number=f"SERIAL{printer_id}",
        ip_address="127.0.0.1",
        access_code="0000",
    )
    session.add(printer)
    await session.flush()
    return printer


async def _seed_pre_migration_bindings(engine, session_maker) -> tuple[int, int]:
    """Spool X bound to TWO slots (older row id 10 < newer row id 20) plus one clean
    binding for spool Y (row 30). Explicit ids so "newest = MAX(id)" is decidable."""
    async with session_maker() as session:
        await _make_printer(session)
        duped = await _make_spool(session, material="PLA")
        clean = await _make_spool(session, material="PETG")
        await session.commit()
        duped_id, clean_id = duped.id, clean.id

    async with engine.begin() as conn:
        for row_id, spool_id, tray_id in ((10, duped_id, 0), (20, duped_id, 1), (30, clean_id, 2)):
            await conn.execute(
                text(
                    "INSERT INTO spool_assignment (id, spool_id, printer_id, ams_id, tray_id) "
                    "VALUES (:id, :spool_id, 1, 0, :tray_id)"
                ),
                {"id": row_id, "spool_id": spool_id, "tray_id": tray_id},
            )
    return duped_id, clean_id


@pytest.mark.asyncio
async def test_duplicate_bindings_deduped_to_newest_then_index_created(engine, session_maker, caplog):
    """Pre-existing duplicates (S10) are dropped keeping MAX(id) — the most recent
    physical observation — each drop WARN-logged, untouched spools left alone, and the
    unique index lands afterwards and is enforced."""
    await _downgrade_spool_assignment(engine)
    duped_id, clean_id = await _seed_pre_migration_bindings(engine, session_maker)

    async with engine.connect() as conn:
        assert await _has_unique_spool_index(conn) is False, "pre-migration schema must not have the index"
        assert len(await _binding_rows(conn)) == 3

    with caplog.at_level(logging.WARNING, logger=_DB_LOGGER):
        async with engine.begin() as conn:
            await run_migrations(conn)

    async with engine.connect() as conn:
        rows = await _binding_rows(conn)
        assert await _has_unique_spool_index(conn) is True

    assert rows == [
        (20, duped_id, 1, 0, 1),  # newest binding of the duplicated spool survived
        (30, clean_id, 1, 0, 2),  # the clean binding is untouched
    ]

    warnings = [r.getMessage() for r in caplog.records if _DUP_WARNING in r.getMessage()]
    assert len(warnings) == 1, "exactly the one doomed row is warned about"
    assert warnings[0].startswith(f"{_DUP_WARNING}: spool {duped_id} -> printer 1 AMS0-T0 (row 10, created ")
    assert warnings[0].endswith("newest binding kept")
    assert f"spool {clean_id} ->" not in warnings[0], "the clean binding is never named as doomed"

    # Fail-loud afterwards: a second binding for the untouched spool is refused.
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO spool_assignment (spool_id, printer_id, ams_id, tray_id) VALUES (:sid, 1, 0, 3)"),
                {"sid": clean_id},
            )


@pytest.mark.asyncio
async def test_binding_dedupe_is_idempotent(engine, session_maker, caplog):
    """A second pass deletes nothing more and warns about nothing (the table already
    holds one row per spool, and CREATE UNIQUE INDEX IF NOT EXISTS is a no-op)."""
    await _downgrade_spool_assignment(engine)
    await _seed_pre_migration_bindings(engine, session_maker)

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        first_pass = await _binding_rows(conn)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_DB_LOGGER):
        async with engine.begin() as conn:
            await run_migrations(conn)
    async with engine.connect() as conn:
        second_pass = await _binding_rows(conn)
        assert await _has_unique_spool_index(conn) is True

    assert second_pass == first_pass, "the second pass must delete nothing further"
    assert [r.getMessage() for r in caplog.records if _DUP_WARNING in r.getMessage()] == []


# ---------------------------------------------------------------------------
# W2 (spool-core re-architecture): last-location residue, pre_configured_at
# backfill, orphaned-binding purge
# ---------------------------------------------------------------------------

_LAST_LOCATION_COLUMNS = (
    "last_location_printer_id",
    "last_location_ams_id",
    "last_location_tray_id",
    "last_location_at",
)


async def _assignment_columns(conn) -> set[str]:
    rows = (await conn.execute(text("PRAGMA table_info(spool_assignment)"))).fetchall()
    return {row[1] for row in rows}


@pytest.mark.asyncio
async def test_migration_readds_the_five_w2_columns(engine):
    """Dropping the five columns simulates a pre-W2 schema; run_migrations re-adds all
    of them (four on spool, one on spool_assignment)."""
    async with engine.begin() as conn:
        for column in _LAST_LOCATION_COLUMNS:
            await conn.execute(text(f"ALTER TABLE spool DROP COLUMN {column}"))
        await conn.execute(text("ALTER TABLE spool_assignment DROP COLUMN pre_configured_at"))
        assert not (await _spool_columns(conn)) & set(_LAST_LOCATION_COLUMNS)
        assert "pre_configured_at" not in await _assignment_columns(conn)

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert set(_LAST_LOCATION_COLUMNS) <= await _spool_columns(conn)
        assert "pre_configured_at" in await _assignment_columns(conn)


@pytest.mark.asyncio
async def test_blank_fingerprint_rows_are_backfilled_as_pre_configured(engine, session_maker):
    """Pre-config intent used to be INFERRED from a blank fingerprint_type. Every row
    carrying that shape today means exactly that, so the migration migrates it to the
    explicit column — and leaves ordinary location claims (a real fingerprint) NULL."""
    from backend.app.models.spool_assignment import SpoolAssignment

    async with session_maker() as session:
        await _make_printer(session)
        blank = await _make_spool(session, material="PLA")
        empty_string = await _make_spool(session, material="PETG")
        claimed = await _make_spool(session, material="ABS")
        session.add_all(
            [
                SpoolAssignment(spool_id=blank.id, printer_id=1, ams_id=0, tray_id=0, fingerprint_type=None),
                SpoolAssignment(spool_id=empty_string.id, printer_id=1, ams_id=0, tray_id=1, fingerprint_type=""),
                SpoolAssignment(spool_id=claimed.id, printer_id=1, ams_id=0, tray_id=2, fingerprint_type="PETG"),
            ]
        )
        await session.commit()

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT tray_id, pre_configured_at FROM spool_assignment ORDER BY tray_id"))
        ).fetchall()

    assert rows[0][1] is not None, "NULL fingerprint == the old pre-configured inference"
    assert rows[1][1] is not None, "empty-string fingerprint too"
    assert rows[2][1] is None, "a row with a real fingerprint is an ordinary location claim"


@pytest.mark.asyncio
async def test_pre_configured_backfill_never_restamps(engine, session_maker):
    """Idempotent by the IS NULL guard: a second pass must not move a stamp already
    written (a re-run would otherwise re-date every pre-config on every restart)."""
    from backend.app.models.spool_assignment import SpoolAssignment

    async with session_maker() as session:
        await _make_printer(session)
        spool = await _make_spool(session, material="PLA")
        session.add(SpoolAssignment(spool_id=spool.id, printer_id=1, ams_id=0, tray_id=0, fingerprint_type=""))
        await session.commit()

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        first = (await conn.execute(text("SELECT pre_configured_at FROM spool_assignment"))).scalar_one()

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        second = (await conn.execute(text("SELECT pre_configured_at FROM spool_assignment"))).scalar_one()

    assert first is not None
    assert second == first


@pytest.mark.asyncio
async def test_orphaned_bindings_are_purged_and_counted(engine, session_maker, caplog):
    """PRE-EXISTING bug: ``DELETE /printers/{id}`` never removed the printer's
    SpoolAssignment rows and SQLite enforces no FK cascade, so every printer ever
    deleted left invisible bindings that still held their spools "assigned". The
    migration clears the rows already orphaned — and only those."""
    from backend.app.models.spool_assignment import SpoolAssignment

    async with session_maker() as session:
        await _make_printer(session, printer_id=1)
        live = await _make_spool(session, material="PLA")
        orphaned = await _make_spool(session, material="PETG")
        session.add_all(
            [
                SpoolAssignment(spool_id=live.id, printer_id=1, ams_id=0, tray_id=0),
                SpoolAssignment(spool_id=orphaned.id, printer_id=42, ams_id=0, tray_id=1),  # printer 42 is gone
            ]
        )
        await session.commit()
        live_id, orphaned_id = live.id, orphaned.id

    with caplog.at_level(logging.INFO, logger=_DB_LOGGER):
        async with engine.begin() as conn:
            await run_migrations(conn)

    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT spool_id, printer_id FROM spool_assignment ORDER BY spool_id"))
        ).fetchall()

    assert [tuple(r) for r in rows] == [(live_id, 1)], "the live binding survives, the orphan is gone"
    purge_lines = [r.getMessage() for r in caplog.records if "orphaned spool binding" in r.getMessage()]
    assert len(purge_lines) == 1
    assert purge_lines[0].startswith("purging 1 orphaned spool binding(s)")
    assert f"spool {orphaned_id}→printer 42" in purge_lines[0]


@pytest.mark.asyncio
async def test_orphan_purge_is_silent_and_idempotent_on_a_clean_table(engine, session_maker, caplog):
    """A clean table matches zero rows: no deletions, and no log noise on every
    restart of a healthy install."""
    from backend.app.models.spool_assignment import SpoolAssignment

    async with session_maker() as session:
        await _make_printer(session)
        spool = await _make_spool(session, material="PLA")
        session.add(SpoolAssignment(spool_id=spool.id, printer_id=1, ams_id=0, tray_id=0))
        await session.commit()

    async with engine.begin() as conn:
        await run_migrations(conn)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_DB_LOGGER):
        async with engine.begin() as conn:
            await run_migrations(conn)

    async with engine.connect() as conn:
        count = (await conn.execute(text("SELECT COUNT(*) FROM spool_assignment"))).scalar_one()

    assert count == 1
    assert [r.getMessage() for r in caplog.records if "orphaned spool binding" in r.getMessage()] == []


# ---------------------------------------------------------------------------
# WS7: sibling_tag_uid column + the false-spent repair
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_readds_sibling_tag_uid_column(engine):
    """Dropping the column simulates a pre-migration schema; run_migrations re-adds it."""
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE spool DROP COLUMN sibling_tag_uid"))
        assert "sibling_tag_uid" not in await _spool_columns(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        assert "sibling_tag_uid" in await _spool_columns(conn)


async def _seed_false_spent_fixtures(session_maker):
    """The three shapes the repair must tell apart. Returns their ids.

    * ``false_spent`` — the prod 185/205 shape: stamped spent, never fed
      (``last_used`` NULL, 0 g used), still BOUND, not archived. Functionality-blocking.
    * ``genuine`` — stamped spent WITH feed evidence. Must never be cleared: doctrine
      rule 8 makes ``last_used`` the evidence field that survives repairs.
    * ``unbound`` — false-spent but bound to nothing. Cosmetic history, deliberately
      out of scope: it blocks no slot.
    """
    from backend.app.models.spool_assignment import SpoolAssignment

    stamped = datetime(2026, 7, 31, 12, 0, 0)
    async with session_maker() as session:
        await _make_printer(session)
        false_spent = await _make_spool(session, spent_at=stamped, last_used=None, weight_used=0.0)
        genuine = await _make_spool(
            session, spent_at=stamped, last_used=datetime(2026, 7, 30, 9, 0, 0), weight_used=900.0
        )
        unbound = await _make_spool(session, spent_at=stamped, last_used=None, weight_used=0.0)
        session.add(SpoolAssignment(spool_id=false_spent.id, printer_id=1, ams_id=0, tray_id=0))
        session.add(SpoolAssignment(spool_id=genuine.id, printer_id=1, ams_id=0, tray_id=1))
        await session.commit()
        return {"false_spent": false_spent.id, "genuine": genuine.id, "unbound": unbound.id}


async def _spent_map(conn) -> dict[int, object]:
    rows = (await conn.execute(text("SELECT id, spent_at FROM spool"))).fetchall()
    return {r[0]: r[1] for r in rows}


@pytest.mark.asyncio
async def test_repair_clears_only_the_bound_never_fed_false_spent_row(engine, session_maker, caplog):
    """A spool that 'ran out' but never fed is self-contradictory — and while it is
    BOUND it blocks its slot forever (a same-roll discovery read concludes KEEP on the
    spent latch, and selection hard-excludes spent rows). Exactly that row is cleared."""
    ids = await _seed_false_spent_fixtures(session_maker)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_DB_LOGGER):
        async with engine.begin() as conn:
            await run_migrations(conn)

    async with engine.connect() as conn:
        spent = await _spent_map(conn)

    assert spent[ids["false_spent"]] is None, "the 185/205 shape is cleared"
    assert spent[ids["genuine"]] is not None, "feed evidence (rule 8) protects a real stamp"
    assert spent[ids["unbound"]] is not None, "unbound false-spent is cosmetic and stays"

    repair_lines = [r.getMessage() for r in caplog.records if "[REPAIR] clearing false spent stamps" in r.getMessage()]
    assert len(repair_lines) == 1
    assert str(ids["false_spent"]) in repair_lines[0]


@pytest.mark.asyncio
async def test_repair_is_idempotent_and_silent_on_a_second_run(engine, session_maker, caplog):
    """Second run matches nothing (its own predicate requires a standing stamp), and an
    empty result logs nothing at all."""
    ids = await _seed_false_spent_fixtures(session_maker)

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        first_pass = await _spent_map(conn)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_DB_LOGGER):
        async with engine.begin() as conn:
            await run_migrations(conn)
    async with engine.connect() as conn:
        second_pass = await _spent_map(conn)

    assert first_pass == second_pass
    assert first_pass[ids["genuine"]] is not None
    assert [r.getMessage() for r in caplog.records if "[REPAIR]" in r.getMessage()] == []


@pytest.mark.asyncio
async def test_repair_unblocks_the_slot_for_selection(engine, session_maker):
    """LIVENESS PIN. Clearing the stamp is only the mechanism; the CONSEQUENCE is that
    the slot becomes selectable again. Asserted through the real consumer
    (``spool_selection.build_slot_inventory``), because a suppression/repair fix that is
    verified only by absence cannot tell a cure from a deeper deadlock."""
    from unittest.mock import AsyncMock, patch

    from backend.app.models.spool import Spool
    from backend.app.services.spool_selection import build_slot_inventory

    ids = await _seed_false_spent_fixtures(session_maker)
    loaded = [{"ams_id": 0, "tray_id": 0, "global_tray_id": 0, "is_external": False}]

    async with session_maker() as session:
        with patch("backend.app.services.spool_selection._is_spoolman_mode", new=AsyncMock(return_value=False)):
            before = await build_slot_inventory(session, printer_id=1, loaded=loaded)
    assert before[0].spent is True, "pre-repair the slot is hard-excluded from selection"

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with session_maker() as session:
        with patch("backend.app.services.spool_selection._is_spoolman_mode", new=AsyncMock(return_value=False)):
            after = await build_slot_inventory(session, printer_id=1, loaded=loaded)
    assert after[0].spent is False, "post-repair the roll is eligible again"
    # Still the SAME binding, with its GRAM LEDGER intact — the repair clears one stamp,
    # it does not rebind the slot or touch grams. Remaining grams are DERIVED from the
    # stamp (:attr:`Spool.remaining_g`), so the published figure legitimately moves:
    # empty while stamped, back to the untouched ledger once cleared. Computing the
    # expected figure from the raw columns is what makes this lossless-ness, not a
    # tautology — a derivation that floored storage could not produce it.
    async with session_maker() as session:
        row = await session.get(Spool, ids["false_spent"])
        ledger_remaining = float(row.label_weight) - float(row.weight_used)
    assert ledger_remaining > 0, "fixture sanity: the false-spent row still carries grams"
    assert before[0].remaining_g == 0.0, "a spent row prices as empty however full its ledger"
    assert after[0].remaining_g == ledger_remaining, "clearing the stamp restores the intact ledger"


# ---------------------------------------------------------------------------
# WS-G: archive the UNBOUND phantom-presence ams_auto spools (2026-08-09/10)
# ---------------------------------------------------------------------------

_PHANTOM_ERA = datetime(2026, 8, 9, 14, 30, 0)  # inside the measured phantom window
_PRE_PHANTOM_ERA = datetime(2026, 8, 8, 23, 59, 59)  # one second before it opens
_PHANTOM_LOG = "[REPAIR] archiving unbound phantom-presence ams_auto spools"


async def _make_dated_spool(session, *, created_at, **kwargs):
    """``_make_spool`` pins ``created_at`` itself, so the era fence needs it re-stamped."""
    spool = await _make_spool(session, **kwargs)
    spool.created_at = created_at
    await session.flush()
    return spool


async def _seed_phantom_fixtures(session_maker):
    """The qualifying row plus one row per FENCE, each differing from the qualifying
    shape in EXACTLY ONE attribute. Isolating them matters: if a sibling row tripped two
    fences at once, dropping either fence from the predicate would still leave the suite
    green. Returns the ids.

    * ``unbound`` — the phantom shape: ``ams_auto``, 0 g, never fed, not spent, not
      archived, minted in the phantom era, bound to NOTHING. No runtime path will ever
      touch it again, so only a migration can clear it.
    * ``bound`` — byte-identical but BOUND. Deliberately out of scope: the presence fix
      makes the runtime release and dispose it with a full log trail.
    * ``spent`` — stamped spent. Ruling R2 forbids every spent-adjacent mutation.
    * ``fed`` — carries a ``last_used`` feed stamp (doctrine rule 8's evidence field).
    * ``charged`` — 42 g on the ledger: real consumption above the never-fed floor.
    * ``operator`` — an operator-created (``manual``) row awaiting its roll. Auto-minted
      origin is what identifies a phantom; hand-made inventory is untouchable.
    * ``pre_era`` — otherwise qualifying but minted BEFORE 2026-08-09.
    """
    from backend.app.models.spool_assignment import SpoolAssignment

    phantom = {"data_origin": "ams_auto", "weight_used": 0.0, "last_used": None}
    async with session_maker() as session:
        await _make_printer(session)
        unbound = await _make_dated_spool(session, created_at=_PHANTOM_ERA, **phantom)
        bound = await _make_dated_spool(session, created_at=_PHANTOM_ERA, **phantom)
        spent = await _make_dated_spool(
            session, created_at=_PHANTOM_ERA, **{**phantom, "spent_at": datetime(2026, 8, 9, 15, 0, 0)}
        )
        fed = await _make_dated_spool(
            session, created_at=_PHANTOM_ERA, **{**phantom, "last_used": datetime(2026, 8, 9, 16, 0, 0)}
        )
        charged = await _make_dated_spool(session, created_at=_PHANTOM_ERA, **{**phantom, "weight_used": 42.0})
        operator = await _make_dated_spool(session, created_at=_PHANTOM_ERA, **{**phantom, "data_origin": "manual"})
        pre_era = await _make_dated_spool(session, created_at=_PRE_PHANTOM_ERA, **phantom)
        session.add(SpoolAssignment(spool_id=bound.id, printer_id=1, ams_id=0, tray_id=0))
        await session.commit()
        return {
            "unbound": unbound.id,
            "bound": bound.id,
            "spent": spent.id,
            "fed": fed.id,
            "charged": charged.id,
            "operator": operator.id,
            "pre_era": pre_era.id,
        }


async def _archived_map(conn) -> dict[int, object]:
    rows = (await conn.execute(text("SELECT id, archived_at FROM spool"))).fetchall()
    return {r[0]: r[1] for r in rows}


@pytest.mark.asyncio
async def test_repair_archives_only_the_unbound_never_fed_phantom_row(engine, session_maker, caplog):
    """A phantom-presence mint that nothing binds is unreachable clutter — no runtime lane
    observes it, so corrected code can never self-heal it. Exactly that row is archived,
    and every fence row survives untouched."""
    ids = await _seed_phantom_fixtures(session_maker)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_DB_LOGGER):
        async with engine.begin() as conn:
            await run_migrations(conn)

    async with engine.connect() as conn:
        archived = await _archived_map(conn)

    assert archived[ids["unbound"]] is not None, "the unbound phantom is archived"
    assert archived[ids["bound"]] is None, "a bound phantom belongs to the runtime disposal lane"
    assert archived[ids["spent"]] is None, "ruling R2: never touch a spent row"
    assert archived[ids["fed"]] is None, "feed evidence (rule 8) protects a real roll"
    assert archived[ids["charged"]] is None, "grams on the ledger are real consumption"
    assert archived[ids["operator"]] is None, "hand-made inventory is not auto-minted clutter"
    assert archived[ids["pre_era"]] is None, "the blast radius stops at the measured era"

    lines = [r.getMessage() for r in caplog.records if _PHANTOM_LOG in r.getMessage()]
    assert len(lines) == 1, "the matched ids are logged once, BEFORE the mutation"
    assert str(ids["unbound"]) in lines[0]
    assert str(ids["bound"]) not in lines[0]


@pytest.mark.asyncio
async def test_phantom_archive_is_idempotent_and_silent_on_a_second_run(engine, session_maker, caplog):
    """Second run matches nothing (its own predicate requires ``archived_at IS NULL``), so
    the stamp is never rewritten and a healthy install logs nothing on every restart."""
    ids = await _seed_phantom_fixtures(session_maker)

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        first_pass = await _archived_map(conn)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_DB_LOGGER):
        async with engine.begin() as conn:
            await run_migrations(conn)
    async with engine.connect() as conn:
        second_pass = await _archived_map(conn)

    assert second_pass == first_pass, "no row changed, and the original stamp was not rewritten"
    assert first_pass[ids["unbound"]] is not None
    assert [r.getMessage() for r in caplog.records if _PHANTOM_LOG in r.getMessage()] == []


@pytest.mark.asyncio
async def test_archived_phantom_leaves_the_operator_inventory_list(engine, session_maker):
    """LIVENESS PIN. Clearing the column is only the mechanism; the CONSEQUENCE the repair
    exists for is that the clutter leaves the operator's inventory. Asserted through the
    real consumer (``inventory.list_spools``), because a repair verified only by its own
    column read cannot tell a cure from a predicate that matched nothing."""
    from backend.app.api.routes.inventory import list_spools

    ids = await _seed_phantom_fixtures(session_maker)

    async with session_maker() as session:
        before = {s.id for s in await list_spools(include_archived=False, db=session, _=None)}
    assert ids["unbound"] in before, "pre-repair the phantom clutters the active inventory"

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with session_maker() as session:
        after = {s.id for s in await list_spools(include_archived=False, db=session, _=None)}
        with_archived = {s.id for s in await list_spools(include_archived=True, db=session, _=None)}

    assert ids["unbound"] not in after, "post-repair the phantom is gone from the active list"
    assert before - after == {ids["unbound"]}, "and it is the ONLY row the operator loses"
    assert ids["unbound"] in with_archived, "archived is a soft-hide — the row is kept, never deleted"
