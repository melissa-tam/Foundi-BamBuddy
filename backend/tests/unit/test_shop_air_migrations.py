"""Migrations of the one-value eject line (2026-09-25).

Three, all in ``run_migrations``:

* the shop-air sample cache is BOOTSTRAPPED from the last 7 days of
  ``printer_sensor_history`` through the same qualification the live writer runs, keyed
  by the rule's version (a settings marker) — built once, idempotent, rebuilt from scratch
  on a version change, and never fatal;
* ``eject_profiles.cooldown_temp_c`` — ``FLOAT NOT NULL`` with no DDL default — is rebuilt
  NULLABLE from its live DDL (expand/contract: the column stays for the rollback build,
  and with the model no longer naming it every new-profile INSERT would otherwise fail);
* the retired ``farm_cooldown_warn_floor_c`` settings row is deleted.

The history seeded here is the ``at_rest`` cut of the production export
(``services/fixtures/shop_air/sensor_cuts.json``), moved in time so its evaluated minute
sits 30 minutes before the migration's clock.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.core.database import _rebuild_column_nullable, run_migrations
from backend.app.models.eject_profile import EjectProfile
from backend.app.models.printer import Printer
from backend.app.models.printer_sensor_history import PrinterSensorHistory
from backend.app.services.eject import shop_air
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")

_CUTS = Path(__file__).parent / "services" / "fixtures" / "shop_air" / "sensor_cuts.json"
_KINDS = ("bed", "chamber", "nozzle", "nozzle_2")
_MARKER = "shop_air_sample_rule_version"


@pytest.fixture
async def engine():
    eng = await create_memory_engine()
    yield eng
    await eng.dispose()


async def _seed_at_rest_history(eng) -> datetime:
    """One H2S printer and the at_rest cut, ending 30 min ago. Returns the evaluated minute."""
    cut = next(c for c in json.loads(_CUTS.read_text(encoding="utf-8"))["cuts"] if c["name"] == "at_rest")
    evaluated = datetime.fromisoformat(cut["evaluated_at"])
    now = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
    shift = (now - timedelta(minutes=30)) - evaluated
    maker = async_sessionmaker(eng, expire_on_commit=False)
    async with maker() as db:
        db.add(Printer(id=1, name="air", serial_number="SAIR", ip_address="10.0.0.1", access_code="x", model="H2S"))
        for row in cut["rows"]:
            at = datetime.fromisoformat(row[0]) + shift
            for index, kind in enumerate(_KINDS):
                value, target = row[1 + 2 * index], row[2 + 2 * index]
                if value is None and target is None:
                    continue
                db.add(PrinterSensorHistory(printer_id=1, sensor_kind=kind, value=value, target=target, recorded_at=at))
        await db.commit()
    return evaluated + shift


async def _samples(eng) -> list[tuple]:
    async with eng.connect() as conn:
        rows = await conn.execute(
            text("SELECT id, printer_id, recorded_at, value_c, rule_version FROM shop_air_sample ORDER BY id")
        )
        return [tuple(row) for row in rows]


async def _marker(eng) -> str | None:
    async with eng.connect() as conn:
        return (await conn.execute(text("SELECT value FROM settings WHERE key = :k"), {"k": _MARKER})).scalar()


class TestShopAirBackfill:
    async def test_the_backfill_fills_samples_from_the_seeded_history(self, engine):
        evaluated = await _seed_at_rest_history(engine)
        async with engine.begin() as conn:
            await run_migrations(conn)

        samples = await _samples(engine)
        assert samples, "the at-rest minute of the seeded history became a sample"
        assert all(row[1] == 1 and row[4] == shop_air.RULE_VERSION for row in samples)
        assert any(str(row[2]).startswith(evaluated.isoformat(sep=" ")) and row[3] == 29.0 for row in samples)
        assert await _marker(engine) == str(shop_air.RULE_VERSION)

    async def test_a_second_boot_changes_nothing(self, engine):
        await _seed_at_rest_history(engine)
        async with engine.begin() as conn:
            await run_migrations(conn)
        first = await _samples(engine)
        async with engine.begin() as conn:
            await run_migrations(conn)
        assert await _samples(engine) == first  # same rows, same ids — not re-derived

    async def test_a_new_rule_version_drops_the_cache_and_rebuilds_it(self, engine, monkeypatch):
        await _seed_at_rest_history(engine)
        async with engine.begin() as conn:
            await run_migrations(conn)
            # A sample the OLD rule produced and the new one would not: it must not survive.
            await conn.execute(
                text(
                    "INSERT INTO shop_air_sample (printer_id, recorded_at, value_c, rule_version) "
                    "VALUES (1, '2026-09-01 00:00:00.000000', 99.0, :v)"
                ),
                {"v": shop_air.RULE_VERSION},
            )

        monkeypatch.setattr(shop_air, "RULE_VERSION", shop_air.RULE_VERSION + 1)
        async with engine.begin() as conn:
            await run_migrations(conn)

        rebuilt = await _samples(engine)
        assert rebuilt
        assert {row[4] for row in rebuilt} == {shop_air.RULE_VERSION}  # never two rules mixed
        assert 99.0 not in {row[3] for row in rebuilt}  # the old rule's cache is gone, not kept
        assert await _marker(engine) == str(shop_air.RULE_VERSION)

    async def test_a_failed_backfill_never_takes_startup_down(self, engine, monkeypatch):
        await _seed_at_rest_history(engine)

        async def _boom(conn, now):
            raise RuntimeError("history unreadable")

        monkeypatch.setattr(shop_air, "backfill", _boom)
        async with engine.begin() as conn:
            await run_migrations(conn)  # does not raise

        assert await _samples(engine) == []
        assert await _marker(engine) is None  # the next boot retries


_OLD_COLUMN = "cooldown_temp_c FLOAT NOT NULL"


async def _old_eject_profiles_schema(eng) -> None:
    """Rebuild ``eject_profiles`` as an install from before 2026-09-25 has it.

    ``create_all`` builds the table from today's model — no ``cooldown_temp_c`` at all —
    which would mask the migration, so the live DDL gets the old column back as
    ``FLOAT NOT NULL`` with no default, plus a hand-written index and trigger the model does
    not declare (the rebuild must carry both), and a legacy profile row.
    """
    async with eng.begin() as conn:
        ddl = (
            await conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='eject_profiles'"))
        ).scalar()
        assert "AUTOINCREMENT" in ddl.upper()
        old_ddl = ddl.replace("name VARCHAR(100) NOT NULL,", f"name VARCHAR(100) NOT NULL, \n\t{_OLD_COLUMN},", 1)
        assert old_ddl != ddl
        await conn.execute(text("DROP TABLE eject_profiles"))
        await conn.execute(text(old_ddl))
        await conn.execute(text("CREATE INDEX ix_eject_probe_clearance ON eject_profiles (clearance_mm)"))
        await conn.execute(text("CREATE TABLE eject_probe_log (profile_id INTEGER)"))
        await conn.execute(
            text(
                "CREATE TRIGGER eject_probe_insert AFTER INSERT ON eject_profiles "
                "BEGIN INSERT INTO eject_probe_log (profile_id) VALUES (NEW.id); END"
            )
        )
    maker = async_sessionmaker(eng, expire_on_commit=False)
    async with maker() as db:
        await db.execute(
            text(
                "INSERT INTO eject_profiles (id, name, cooldown_temp_c, clearance_mm, z_offset_mm, descent_steps, "
                "x_passes, x_margin_mm, front_overhang_mm, back_overhang_mm, eject_speed_mm_min, skim_speed_mm_min, "
                "max_part_height_mm, sweep_start_frac, final_skim) "
                "VALUES (7, 'legacy', 33, 10, 0.4, 4, 11, 3, 2, 2, 3000, 1500, 42, 1.0, 1)"
            )
        )
        await db.commit()


async def _column(eng, name: str):
    async with eng.connect() as conn:
        rows = (await conn.execute(text("PRAGMA table_info(eject_profiles)"))).fetchall()
    return next((row for row in rows if row[1] == name), None)


class TestEjectProfileColumnRelaxed:
    async def test_on_the_old_schema_a_new_profile_cannot_be_inserted(self, engine):
        """The failure the migration exists for: the model no longer names the column."""
        await _old_eject_profiles_schema(engine)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            db.add(EjectProfile(name="new"))
            with pytest.raises(IntegrityError):
                await db.commit()

    async def test_the_column_is_rebuilt_nullable_and_everything_else_is_carried(self, engine):
        await _old_eject_profiles_schema(engine)
        async with engine.begin() as conn:
            await run_migrations(conn)

        column = await _column(engine, "cooldown_temp_c")
        assert column is not None, "the physical column STAYS (the rollback build reads it)"
        assert column[3] == 0, "and is nullable now"
        assert (await _column(engine, "name"))[3] == 1  # every other constraint verbatim
        async with engine.connect() as conn:
            ddl = (
                await conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='eject_profiles'"))
            ).scalar()
            objects = {
                row[0]
                for row in await conn.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE tbl_name='eject_profiles' AND type IN ('index','trigger')"
                    )
                )
            }
            legacy = (await conn.execute(text("SELECT cooldown_temp_c FROM eject_profiles WHERE id = 7"))).scalar()
        assert "AUTOINCREMENT" in ddl.upper()
        assert {"ix_eject_probe_clearance", "eject_probe_insert"} <= objects
        assert legacy == 33.0  # the legacy row keeps its value for the rollback build

    async def test_create_and_update_work_through_the_route_after_the_rebuild(self, engine):
        """The eject-profile routes' own code on the migrated table: create, then update."""
        from backend.app.api.routes.eject_profiles import create_eject_profile, update_eject_profile
        from backend.app.schemas.eject_profile import EjectProfileCreate, EjectProfileUpdate

        await _old_eject_profiles_schema(engine)
        async with engine.begin() as conn:
            await run_migrations(conn)

        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            created = await create_eject_profile(EjectProfileCreate(name="after"), db=db, _=None)
            assert created.id > 7  # AUTOINCREMENT survived: never a reissued id
            updated = await update_eject_profile(created.id, EjectProfileUpdate(clearance_mm=12.0), db=db, _=None)
            assert updated.clearance_mm == 12.0
        async with engine.connect() as conn:
            fired = (await conn.execute(text("SELECT profile_id FROM eject_probe_log"))).scalars().all()
            stored = (
                await conn.execute(text("SELECT cooldown_temp_c FROM eject_profiles WHERE id = :i"), {"i": created.id})
            ).scalar()
        assert created.id in fired  # the replayed trigger still FIRES
        assert stored is None  # nothing writes the retired column

    async def test_the_rebuild_is_idempotent(self, engine):
        await _old_eject_profiles_schema(engine)
        async with engine.begin() as conn:
            assert await _rebuild_column_nullable(conn, "eject_profiles", "cooldown_temp_c") is True
        async with engine.begin() as conn:
            assert await _rebuild_column_nullable(conn, "eject_profiles", "cooldown_temp_c") is False
            await run_migrations(conn)  # and a whole boot on top changes nothing either
        assert (await _column(engine, "cooldown_temp_c"))[3] == 0

    async def test_an_unretrofitted_table_gets_autoincrement_and_keeps_the_retired_column(self, engine):
        """The interplay with the 2026-09-17 AUTOINCREMENT retrofit: on an install that never
        had it, the retired column is a live column the model lacks — which that rebuild
        would skip the table over, leaving deleted ids reusable. The ledger makes it CARRY
        the column instead: AUTOINCREMENT, the column kept (nullable), its data intact."""
        await _old_eject_profiles_schema(engine)
        async with engine.begin() as conn:
            ddl = (
                await conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='eject_profiles'"))
            ).scalar()
            without_ai = ddl.replace(" AUTOINCREMENT", "").replace(" autoincrement", "")
            assert "AUTOINCREMENT" not in without_ai.upper()
            await conn.execute(text("ALTER TABLE eject_profiles RENAME TO eject_profiles_old"))
            await conn.execute(text(without_ai))
            await conn.execute(text("INSERT INTO eject_profiles SELECT * FROM eject_profiles_old"))
            await conn.execute(text("DROP TABLE eject_profiles_old"))
        async with engine.begin() as conn:
            await run_migrations(conn)

        async with engine.connect() as conn:
            ddl = (
                await conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='eject_profiles'"))
            ).scalar()
            legacy = (await conn.execute(text("SELECT cooldown_temp_c FROM eject_profiles WHERE id = 7"))).scalar()
        assert "AUTOINCREMENT" in ddl.upper()
        assert (await _column(engine, "cooldown_temp_c"))[3] == 0
        assert legacy == 33.0

    async def test_a_fresh_install_has_no_such_column_and_nothing_to_rebuild(self, engine):
        async with engine.begin() as conn:
            assert await _rebuild_column_nullable(conn, "eject_profiles", "cooldown_temp_c") is False
        assert await _column(engine, "cooldown_temp_c") is None


class TestWarnFloorRowDeleted:
    async def test_the_retired_setting_row_is_deleted_idempotently(self, engine):
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO settings (key, value) VALUES ('farm_cooldown_warn_floor_c', '30')"))
        async with engine.begin() as conn:
            await run_migrations(conn)
        async with engine.begin() as conn:
            await run_migrations(conn)
        async with engine.connect() as conn:
            left = (
                await conn.execute(text("SELECT COUNT(*) FROM settings WHERE key = 'farm_cooldown_warn_floor_c'"))
            ).scalar()
        assert left == 0
