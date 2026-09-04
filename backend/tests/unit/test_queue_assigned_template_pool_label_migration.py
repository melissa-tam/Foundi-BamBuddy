"""The ``queue_job_assigned`` template backfill (2026-09-04; the printer-subset POOL wave).

``{target_model}`` used to be filled with a bare model name, so the default copy supplied
the article itself ("from Any {target_model} queue"). A unit can now be targeted at a
PRINTERS pool, which has no single model, so callers pass the whole noun phrase instead
("Any H2S" / "Any of 001-H2S, 003-H2S" — ``DispatchTarget.describe``, the one phrasing).
Left as it was, an install would render "from Any Any of 001-H2S, 003-H2S queue".

``seed_notification_templates`` only INSERTS missing event types, so an install that
already seeded the old default keeps the wrong copy without this backfill — and an admin
who customised the template must keep theirs. Same shape (and the same two cases) as the
sibling ``_migrate_quarantine_template_reason_led`` backfill.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import (
    _QUEUE_ASSIGNED_TEMPLATE_NEW_BODY,
    _QUEUE_ASSIGNED_TEMPLATE_OLD_BODY,
    run_migrations,
)


@pytest.fixture(autouse=True)
def force_sqlite_dialect(monkeypatch):
    """Force the SQLite branch regardless of test env settings."""
    from backend.app.core import db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    from backend.app.core import database as database_module

    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


def _register_all_models():
    """Import EVERY model module so `create_all` builds the whole schema (see the
    sibling migration tests: `run_migrations` ALTERs across the schema and
    `_safe_execute` re-raises "no such table")."""
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


async def _add_template(conn, *, body: str, is_default: bool) -> int:
    from backend.app.models.notification_template import NotificationTemplate

    result = await conn.execute(
        NotificationTemplate.__table__.insert().values(
            event_type="queue_job_assigned",
            name="Queue Job Assigned",
            title_template="Job Assigned",
            body_template=body,
            is_default=is_default,
        )
    )
    return result.inserted_primary_key[0]


async def _body(conn, template_id: int) -> str:
    return (
        await conn.execute(text("SELECT body_template FROM notification_templates WHERE id = :i"), {"i": template_id})
    ).scalar()


@pytest.mark.asyncio
async def test_rewrites_the_untouched_default_body(engine):
    async with engine.begin() as conn:
        template = await _add_template(conn, body=_QUEUE_ASSIGNED_TEMPLATE_OLD_BODY, is_default=True)

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _body(conn, template) == _QUEUE_ASSIGNED_TEMPLATE_NEW_BODY
        assert "Any" not in await _body(conn, template), "the article now comes from the caller's noun phrase"


@pytest.mark.asyncio
async def test_leaves_a_template_the_admin_customised_alone(engine):
    """``is_default`` cleared: the admin owns this row's wording, wrong article or not."""
    async with engine.begin() as conn:
        template = await _add_template(conn, body=_QUEUE_ASSIGNED_TEMPLATE_OLD_BODY, is_default=False)

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _body(conn, template) == _QUEUE_ASSIGNED_TEMPLATE_OLD_BODY


@pytest.mark.asyncio
async def test_leaves_a_default_row_carrying_edited_copy_alone(engine):
    """``is_default`` set but the body no longer matches the shipped default — an edit
    the flag did not follow. Both halves of the predicate are load-bearing."""
    edited = "{job_name} -> {printer} (queue: Any {target_model})"
    async with engine.begin() as conn:
        template = await _add_template(conn, body=edited, is_default=True)

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _body(conn, template) == edited


@pytest.mark.asyncio
async def test_is_idempotent_on_a_second_pass(engine):
    """Self-predicating: the second run matches nothing because the first rewrote it."""
    async with engine.begin() as conn:
        template = await _add_template(conn, body=_QUEUE_ASSIGNED_TEMPLATE_OLD_BODY, is_default=True)

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        assert await _body(conn, template) == _QUEUE_ASSIGNED_TEMPLATE_NEW_BODY
