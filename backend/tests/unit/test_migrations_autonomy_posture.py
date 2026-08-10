"""Regression tests for the WS-F autonomy migrations (2026-08-10).

Two startup migrations, both of which change operator-visible posture and so must
be pinned:

1. Success-class spool-recovery notifications default OFF on EXISTING provider
   rows. A recovery that SUCCEEDED asked nothing of a human, so it is a log line.
   This one is deliberately ONE-TIME rather than idempotent-by-repetition: a plain
   UPDATE would re-run at every boot and silently undo an operator who re-enabled
   the toggle in the UI, turning "the capability remains" into a lie. The durable
   marker is a settings row written in the same nested transaction.

2. The retired ``auto_add_untagged`` key is deleted from the settings table. It
   was a kill switch for the tagless auto-mint lane (doctrine rules 1/2/4); with
   the schema field, route whitelist and Settings toggle gone, a stored row could
   no longer be seen or changed, so leaving one behind would strand an install with
   the lane permanently off. Its reader maps a MISSING key to True, which is what
   makes the deletion restore the shipped default — asserted here directly, because
   the reader itself lives in a module this change may not touch.

SQLite-safe and self-contained, mirroring the sibling migration regression tests.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import run_migrations

_POSTURE_MARKER = "migration_success_notifications_off_20260810"
_SUCCESS_COLUMNS = ("on_spool_recovery_succeeded", "on_spool_recovery_self_healed")
# Stays ON: somebody has to act on a failure.
_FAILURE_COLUMN = "on_spool_recovery_failed"


@pytest.fixture(autouse=True)
def force_sqlite_dialect(monkeypatch):
    """Force the SQLite branch regardless of test env settings."""
    from backend.app.core import db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    from backend.app.core import database as database_module

    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


def _register_all_models():
    """Import EVERY model module so `create_all` builds the whole schema.

    Not a hand-picked subset, and not the package `__init__` either (it does not
    re-export every module — `virtual_printer` is one it misses). `run_migrations`
    ALTERs tables across the schema and `_safe_execute` deliberately RE-RAISES
    "no such table" (that is schema corruption, not idempotency), so a partial
    `create_all` leaves this file passing or failing according to which other test
    module happened to import a model first.
    """
    import importlib
    import pkgutil

    import backend.app.models as models_pkg

    for module in pkgutil.iter_modules(models_pkg.__path__):
        importlib.import_module(f"{models_pkg.__name__}.{module.name}")


@pytest.fixture
async def engine():
    from backend.app.core.database import Base

    _register_all_models()

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


async def _insert_provider(conn, name: str, *, succeeded: int = 1, self_healed: int = 1, failed: int = 1):
    await conn.execute(
        text(
            "INSERT INTO notification_providers "
            "(name, provider_type, config, on_spool_recovery_succeeded, "
            " on_spool_recovery_self_healed, on_spool_recovery_failed) "
            "VALUES (:n, 'webhook', '{}', :s, :h, :f)"
        ),
        {"n": name, "s": succeeded, "h": self_healed, "f": failed},
    )


async def _toggles(conn, name: str) -> dict[str, int]:
    cols = ", ".join((*_SUCCESS_COLUMNS, _FAILURE_COLUMN))
    row = (
        await conn.execute(
            text(f"SELECT {cols} FROM notification_providers WHERE name = :n"), {"n": name}
        )
    ).fetchone()
    return dict(zip((*_SUCCESS_COLUMNS, _FAILURE_COLUMN), row, strict=True))


@pytest.mark.asyncio
async def test_flips_success_toggles_off_on_existing_rows(engine):
    async with engine.begin() as conn:
        await _insert_provider(conn, "legacy")
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        toggles = await _toggles(conn, "legacy")
    assert toggles["on_spool_recovery_succeeded"] == 0
    assert toggles["on_spool_recovery_self_healed"] == 0
    # The failure-class toggle is untouched — those still need a human.
    assert toggles["on_spool_recovery_failed"] == 1


@pytest.mark.asyncio
async def test_records_a_durable_marker_so_it_runs_once(engine):
    async with engine.begin() as conn:
        await _insert_provider(conn, "legacy")
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        marker = (
            await conn.execute(text("SELECT value FROM settings WHERE key = :k"), {"k": _POSTURE_MARKER})
        ).scalar()
    assert marker == "true"


@pytest.mark.asyncio
async def test_never_re_reverts_an_operator_re_enable(engine):
    """THE point of the marker. The operator turns the toggle back on in the UI;
    the next restart must leave it on, or the "re-enable per provider" capability
    is fiction."""
    async with engine.begin() as conn:
        await _insert_provider(conn, "legacy")
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE notification_providers SET on_spool_recovery_succeeded = 1 WHERE name = 'legacy'")
        )

    # Reboot.
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert (await _toggles(conn, "legacy"))["on_spool_recovery_succeeded"] == 1


@pytest.mark.asyncio
async def test_second_pass_is_a_no_op_on_a_row_created_after_the_flip(engine):
    """A provider added later takes the MODEL default (also off) rather than being
    swept by a re-run — the migration is history, not policy enforcement."""
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        # Explicitly subscribed at creation time, the way the UI would.
        await _insert_provider(conn, "new-provider", succeeded=1, self_healed=1)
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        toggles = await _toggles(conn, "new-provider")
    assert toggles["on_spool_recovery_succeeded"] == 1
    assert toggles["on_spool_recovery_self_healed"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["true", "false"])
async def test_deletes_the_retired_auto_add_untagged_row(engine, stored):
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO settings (key, value) VALUES ('auto_add_untagged', :v)"), {"v": stored}
        )
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        remaining = (
            await conn.execute(text("SELECT value FROM settings WHERE key = 'auto_add_untagged'"))
        ).scalar()
    assert remaining is None


def test_no_reader_of_the_retired_key_survives():
    """The retired kill switch is gone END-TO-END: the tagless auto-mint lane is
    unconditional (doctrine rules 1/2/4 make it load-bearing), so no code may read
    the `auto_add_untagged` key again — a resurrected reader would silently gate
    gram tracking for every untagged roll on a value nothing can set."""
    import pathlib

    services = pathlib.Path("backend/app/services")
    offenders = [
        p.name
        for p in services.glob("*.py")
        if "auto_add_untagged" in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert offenders == []
