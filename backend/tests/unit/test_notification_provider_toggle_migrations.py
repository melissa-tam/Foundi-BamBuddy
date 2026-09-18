"""The ``notification_providers`` per-event toggle columns, one migration each.

Every toggle is a boolean column ``run_migrations`` adds through ``_safe_execute``
(ADD COLUMN with a default). ``create_all`` builds the column from the CURRENT model and
would mask the migration entirely, so the fixture drops it again to reconstruct the
pre-migration schema — that is the only reason the fixture exists.

Default polarity is not arbitrary: a toggle whose messages ask a human for something
ships ON, and a success-class toggle ships OFF, because a recovery that worked asked
nothing of anyone and belongs in the log rather than on someone's phone.

A toggle with no seeded template would page nobody — the seeder only INSERTs event types
it finds in ``DEFAULT_TEMPLATES``, and every placeholder the copy uses has to be one the
notification service actually supplies.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import text

from backend.app.core.database import run_migrations
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")

_TABLE = "notification_providers"


@dataclass(frozen=True)
class _Toggle:
    """One provider toggle: the event it subscribes to and the default it ships with."""

    event: str
    default: int
    template_name: str | None = None
    placeholders: frozenset[str] = frozenset()

    @property
    def column(self) -> str:
        return f"on_{self.event}"


_TOGGLES = (
    _Toggle(
        event="ams_wedged_idle",
        default=1,
        template_name="AMS Stuck Mid Filament-Change",
        placeholders=frozenset({"printer_name", "minutes"}),
    ),
    _Toggle(
        event="backup_group_split",
        default=1,
        template_name="AMS Backup Group Split",
        placeholders=frozenset(
            {"printer_name", "slot", "partner_slot", "dimension", "picked_value", "partner_value"}
        ),
    ),
    _Toggle(event="cooldown_escalation", default=1),
    _Toggle(
        event="power_loss_recovery",
        default=1,
        template_name="Power-Loss Recovery",
        placeholders=frozenset({"printer_name", "job_name", "reason", "outage"}),
    ),
    _Toggle(event="spool_recovery_self_healed", default=0),
)

_TEMPLATED = tuple(toggle for toggle in _TOGGLES if toggle.template_name is not None)

# The parametrize id is the event name, which is also the column name minus its ``on_``
# prefix: a case dropped from the table above goes MISSING from the test output rather
# than silently ceasing to be covered.
_per_toggle = pytest.mark.parametrize("toggle", _TOGGLES, ids=lambda toggle: toggle.event)
_per_templated_toggle = pytest.mark.parametrize("toggle", _TEMPLATED, ids=lambda toggle: toggle.event)


@pytest.fixture
async def engine(toggle: _Toggle):
    """The pre-migration schema: everything the current models build, minus this column."""
    eng = await create_memory_engine()
    async with eng.begin() as conn:
        await conn.execute(text(f"ALTER TABLE {_TABLE} DROP COLUMN {toggle.column}"))
    yield eng
    await eng.dispose()


async def _columns(conn) -> set[str]:
    rows = (await conn.execute(text(f"PRAGMA table_info({_TABLE})"))).fetchall()
    return {row[1] for row in rows}


@_per_toggle
async def test_pre_migration_table_lacks_column(toggle: _Toggle, engine):
    """The fixture's simulated old schema really is missing the column."""
    async with engine.connect() as conn:
        assert toggle.column not in await _columns(conn)


@_per_toggle
async def test_migration_adds_column(toggle: _Toggle, engine):
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        assert toggle.column in await _columns(conn)


@_per_toggle
async def test_migration_column_default_matches_event_class(toggle: _Toggle, engine):
    """A row inserted without the toggle gets the default its event class dictates."""
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"INSERT INTO {_TABLE} (name, provider_type, config) "
                "VALUES ('migrated', 'webhook', '{}')"
            )
        )
    async with engine.connect() as conn:
        row = (
            await conn.execute(text(f"SELECT {toggle.column} FROM {_TABLE} WHERE name = 'migrated'"))
        ).fetchone()
    assert row[0] == toggle.default


@_per_toggle
async def test_migration_is_idempotent(toggle: _Toggle, engine):
    """Every boot re-runs the migration list, so a second pass must be a no-op."""
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.connect() as conn:
        assert toggle.column in await _columns(conn)


@_per_templated_toggle
def test_template_is_registered_for_seeding(toggle: _Toggle):
    """The event is seeded with a template whose copy resolves from the supplied fields."""
    from backend.app.models.notification_template import DEFAULT_TEMPLATES

    row = next(template for template in DEFAULT_TEMPLATES if template["event_type"] == toggle.event)
    assert row["name"] == toggle.template_name
    supplied = {field: field for field in toggle.placeholders}
    assert "{" not in row["body_template"].format(**supplied)
    assert "{" not in row["title_template"].format(**supplied)
