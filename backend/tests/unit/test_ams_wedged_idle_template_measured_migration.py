"""The ``ams_wedged_idle`` template backfill.

The 2026-09-11 default body asserted that the AMS "drops every load/unload" while mid
filament-change and told the operator to press Retry/Continue. The wire record supports
only a load into an EMPTY path under the firmware's change-error modal; the premise is
retracted and the default restated as the measured facts plus both exits.

``seed_notification_templates`` only INSERTS missing event types, so an install that
already seeded the old default keeps the retracted copy without this backfill — and an
admin who customised the template must keep theirs. Same shape (and the same cases) as
the sibling ``_migrate_quarantine_template_reason_led`` /
``_migrate_queue_assigned_template_pool_label`` backfills.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import text

from backend.app.core.database import (
    _AMS_WEDGED_IDLE_TEMPLATE_NEW_BODY,
    _AMS_WEDGED_IDLE_TEMPLATE_OLD_BODY,
    run_migrations,
)
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")

_LOGGER = "backend.app.core.database"


@pytest.fixture
async def engine():
    eng = await create_memory_engine()
    yield eng
    await eng.dispose()


async def _add_template(conn, *, body: str, is_default: bool) -> int:
    from backend.app.models.notification_template import NotificationTemplate

    result = await conn.execute(
        NotificationTemplate.__table__.insert().values(
            event_type="ams_wedged_idle",
            name="AMS Stuck Mid Filament-Change",
            title_template="AMS stuck mid filament-change",
            body_template=body,
            is_default=is_default,
        )
    )
    return result.inserted_primary_key[0]


async def _body(conn, template_id: int) -> str:
    return (
        await conn.execute(text("SELECT body_template FROM notification_templates WHERE id = :i"), {"i": template_id})
    ).scalar()


def _migration_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _LOGGER and "ams_wedged_idle" in r.getMessage()]


def test_the_seeded_default_is_the_corrected_body():
    """A fresh install and a migrated install must end on the same copy."""
    from backend.app.models.notification_template import DEFAULT_TEMPLATES

    seeded = next(t for t in DEFAULT_TEMPLATES if t["event_type"] == "ams_wedged_idle")

    assert seeded["body_template"] == _AMS_WEDGED_IDLE_TEMPLATE_NEW_BODY


def test_the_corrected_body_keeps_the_sender_variables_and_drops_the_premise():
    assert "{printer_name}" in _AMS_WEDGED_IDLE_TEMPLATE_NEW_BODY
    assert "{minutes}" in _AMS_WEDGED_IDLE_TEMPLATE_NEW_BODY
    assert "drops every" not in _AMS_WEDGED_IDLE_TEMPLATE_NEW_BODY
    assert "Retry" not in _AMS_WEDGED_IDLE_TEMPLATE_NEW_BODY


@pytest.mark.asyncio
async def test_rewrites_the_untouched_default_body(engine, caplog):
    async with engine.begin() as conn:
        template = await _add_template(conn, body=_AMS_WEDGED_IDLE_TEMPLATE_OLD_BODY, is_default=True)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        async with engine.begin() as conn:
            await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _body(conn, template) == _AMS_WEDGED_IDLE_TEMPLATE_NEW_BODY
    assert len(_migration_lines(caplog)) == 1, "one INFO line on the pass that changes the row"


@pytest.mark.asyncio
async def test_leaves_a_template_the_admin_customised_alone(engine):
    """``is_default`` cleared: the admin owns this row's wording, retracted premise or not."""
    async with engine.begin() as conn:
        template = await _add_template(conn, body=_AMS_WEDGED_IDLE_TEMPLATE_OLD_BODY, is_default=False)

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _body(conn, template) == _AMS_WEDGED_IDLE_TEMPLATE_OLD_BODY


@pytest.mark.asyncio
async def test_leaves_a_default_row_carrying_edited_copy_alone(engine):
    """``is_default`` set but the body no longer matches the shipped default — an edit
    the flag did not follow. Both halves of the predicate are load-bearing."""
    edited = "{printer_name}: AMS mid-change {minutes} min. Check the printer."
    async with engine.begin() as conn:
        template = await _add_template(conn, body=edited, is_default=True)

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _body(conn, template) == edited


@pytest.mark.asyncio
async def test_is_idempotent_on_a_second_pass(engine, caplog):
    """Self-predicating: the second run matches nothing because the first rewrote it,
    and it logs nothing."""
    async with engine.begin() as conn:
        template = await _add_template(conn, body=_AMS_WEDGED_IDLE_TEMPLATE_OLD_BODY, is_default=True)

    async with engine.begin() as conn:
        await run_migrations(conn)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        async with engine.begin() as conn:
            await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _body(conn, template) == _AMS_WEDGED_IDLE_TEMPLATE_NEW_BODY
    assert _migration_lines(caplog) == []
