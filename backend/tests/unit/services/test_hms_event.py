"""The durable HMS vocabulary: what main's HMS block writes, logs and prunes.

Production gap these pin (2026-08-09): HMS codes were persisted nowhere and the
"new codes this cycle" line was DEBUG, which prod does not write — so a code with no
catalog description left ZERO trace, ever. Printer 2 stood on ``0500_0051`` /
``0500_0005`` with zero occurrences across 914k log lines and every incident audit had
to reconstruct the fleet's vocabulary from notification side effects.

These drive the REAL ``main.on_printer_status_change`` through the shared HMS harness
(``test_main_hms_pipeline._Harness``), extended here with a real session factory so the
assertions are about rows the pipeline actually wrote, and a catalog map so a case can
model an UNDESCRIBED code.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app import main as main_module
from backend.app.models.hms_event import HMSEvent
from backend.app.services import notify_dedup
from backend.tests.unit.test_main_hms_pipeline import _Harness, _state

_MAIN_LOGGER = "backend.app.main"

# Two codes live on production printer 2 with NO catalog description — the exact shape
# that used to vanish without trace. attr/code carry the lossless words; full_code is
# the 16-hex identifier the MQTT parser composes.
_UNKNOWN_A = SimpleNamespace(
    code="0x30051", attr=0x05000000, module=0x05, severity=3, full_code="0500000000030051", short="0500_0051"
)
_UNKNOWN_B = SimpleNamespace(
    code="0x30005", attr=0x05000000, module=0x05, severity=3, full_code="0500000000030005", short="0500_0005"
)
# A catalogued code, to pin the negative case of the parenthetical grammar.
_KNOWN = SimpleNamespace(
    code="0x24025", attr=0x07002000, module=0x07, severity=2, full_code="0700200000024025", short="0700_4025"
)
_CATALOG = {_KNOWN.full_code: "AMS main board error"}


def _hms(spec: SimpleNamespace) -> SimpleNamespace:
    """An HMSError-shaped stub (the pipeline reads only these attributes)."""
    return SimpleNamespace(
        code=spec.code, attr=spec.attr, module=spec.module, severity=spec.severity, full_code=spec.full_code
    )


@pytest.fixture(autouse=True)
def _reset_hms_state():
    """The writer's throttle map is process state, like the dedup ledgers around it."""
    notify_dedup._reset_state()
    main_module._hms_event_written_at.clear()
    main_module._printer_last_connected.clear()
    main_module._printer_reconciled_since_connect.clear()
    main_module._last_status_broadcast.clear()
    yield
    notify_dedup._reset_state()
    main_module._hms_event_written_at.clear()
    main_module._printer_last_connected.clear()
    main_module._printer_reconciled_since_connect.clear()
    main_module._last_status_broadcast.clear()


@pytest.fixture
def sessions(test_engine):
    """A real session factory for the pipeline, bound to the test engine."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def _rows(db: AsyncSession, printer_id: int) -> list[HMSEvent]:
    """The printer's vocabulary rows, re-read from the DB.

    ``populate_existing`` (not ``expire_all``) because the pipeline writes through its
    OWN session: this refreshes exactly the rows being asserted on, while expiring the
    whole identity map would make the next ``printer.id`` touch a lazy load from sync
    context (MissingGreenlet).
    """
    result = await db.execute(
        select(HMSEvent)
        .where(HMSEvent.printer_id == printer_id)
        .order_by(HMSEvent.full_code)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


def _rewind_throttle(printer_id: int, full_code: str, seconds: float) -> None:
    """Age a throttle stamp in place — the repo's monotonic idiom (never freeze the
    process-wide clock; it is the running event loop's)."""
    key = (printer_id, full_code)
    main_module._hms_event_written_at[key] = main_module._hms_event_written_at[key] - seconds


@pytest.mark.asyncio
class TestVocabularyIsRecorded:
    async def test_an_uncatalogued_code_gets_a_row_and_names_itself_in_the_info_line(
        self, db_session, printer_factory, sessions, caplog
    ):
        """The whole point: a code the catalog does not know now leaves BOTH a durable
        row and an INFO line carrying its lossless full_code."""
        printer = await printer_factory()
        with (
            _Harness(session_factory=sessions, catalog=_CATALOG) as h,
            caplog.at_level(logging.INFO, logger=_MAIN_LOGGER),
        ):
            await main_module.on_printer_status_change(printer.id, _state([_hms(_UNKNOWN_A)]))

        rows = await _rows(db_session, printer.id)
        assert [r.full_code for r in rows] == [_UNKNOWN_A.full_code]
        assert rows[0].count == 1
        assert rows[0].first_seen == rows[0].last_seen
        # It notified nowhere — an undescribed code never reaches the operator.
        h.notify.on_printer_error.assert_not_awaited()
        line = next(m for m in caplog.messages if "new codes this cycle" in m)
        assert f"{_UNKNOWN_A.short}({_UNKNOWN_A.full_code})" in line

    async def test_a_catalogued_code_logs_without_the_parenthetical(
        self, db_session, printer_factory, sessions, caplog
    ):
        """The parenthetical is the "you cannot look this up" marker, not decoration."""
        printer = await printer_factory()
        with _Harness(session_factory=sessions, catalog=_CATALOG), caplog.at_level(logging.INFO, logger=_MAIN_LOGGER):
            await main_module.on_printer_status_change(printer.id, _state([_hms(_KNOWN)]))

        line = next(m for m in caplog.messages if "new codes this cycle" in m)
        assert _KNOWN.short in line
        assert f"({_KNOWN.full_code})" not in line

    async def test_raw_words_and_severity_are_stored_from_the_one_origin(self, db_session, printer_factory, sessions):
        """attr/code keep the LOSSLESS wire words (the short code drops the attr low
        word and the code high word). Severity is the PARSER's value — bambu_mqtt
        already ran ``hms_errors.hms_severity`` for the hms[] path — so the row can
        never disagree with the severity the notify filter judged."""
        from backend.app.services.hms_errors import hms_severity

        printer = await printer_factory()
        with _Harness(session_factory=sessions, catalog=_CATALOG):
            await main_module.on_printer_status_change(printer.id, _state([_hms(_UNKNOWN_A)]))

        row = (await _rows(db_session, printer.id))[0]
        assert row.attr == _UNKNOWN_A.attr
        assert row.code == 0x30051
        assert row.severity == _UNKNOWN_A.severity == hms_severity(_UNKNOWN_A.code) == 3

    async def test_a_second_distinct_code_gets_its_own_row(self, db_session, printer_factory, sessions):
        printer = await printer_factory()
        with _Harness(session_factory=sessions, catalog=_CATALOG):
            await main_module.on_printer_status_change(printer.id, _state([_hms(_UNKNOWN_A)], layer_num=1))
            await main_module.on_printer_status_change(
                printer.id, _state([_hms(_UNKNOWN_A), _hms(_UNKNOWN_B)], layer_num=2)
            )

        rows = await _rows(db_session, printer.id)
        assert [r.full_code for r in rows] == sorted([_UNKNOWN_A.full_code, _UNKNOWN_B.full_code])
        # The standing code is inside its throttle window; only the newcomer was written.
        assert {r.full_code: r.count for r in rows} == {_UNKNOWN_A.full_code: 1, _UNKNOWN_B.full_code: 1}

    async def test_two_printers_keep_independent_vocabularies(self, db_session, printer_factory, sessions):
        """The row's identity is (printer, code) — the same fault on two machines is two
        rows, because "which printer says this" is the question being asked."""
        one = await printer_factory()
        two = await printer_factory()
        with _Harness(session_factory=sessions, catalog=_CATALOG):
            await main_module.on_printer_status_change(one.id, _state([_hms(_UNKNOWN_A)]))
            await main_module.on_printer_status_change(two.id, _state([_hms(_UNKNOWN_A)]))

        assert [r.printer_id for r in await _rows(db_session, one.id)] == [one.id]
        assert [r.printer_id for r in await _rows(db_session, two.id)] == [two.id]


@pytest.mark.asyncio
class TestWriteThrottle:
    async def test_repeated_pushes_inside_the_window_write_nothing(self, db_session, printer_factory, sessions):
        """A standing code rides EVERY ~1 Hz push; an unthrottled upsert would be a
        write per second per code. Five pushes, one row, count unchanged."""
        printer = await printer_factory()
        with _Harness(session_factory=sessions, catalog=_CATALOG):
            for layer in range(5):
                await main_module.on_printer_status_change(printer.id, _state([_hms(_UNKNOWN_A)], layer_num=layer))

        rows = await _rows(db_session, printer.id)
        assert len(rows) == 1
        assert rows[0].count == 1
        assert rows[0].first_seen == rows[0].last_seen

    async def test_the_window_expiring_re_records_the_code(self, db_session, printer_factory, sessions):
        """Once the window passes, the still-live code is counted again and its
        last_seen advances — that is how "still saying this, N windows later" reads."""
        printer = await printer_factory()
        with _Harness(session_factory=sessions, catalog=_CATALOG):
            await main_module.on_printer_status_change(printer.id, _state([_hms(_UNKNOWN_A)], layer_num=1))
            first = (await _rows(db_session, printer.id))[0]
            first_seen, first_last_seen = first.first_seen, first.last_seen

            _rewind_throttle(printer.id, _UNKNOWN_A.full_code, main_module._HMS_EVENT_WRITE_INTERVAL_S + 1)
            await main_module.on_printer_status_change(printer.id, _state([_hms(_UNKNOWN_A)], layer_num=2))

        rows = await _rows(db_session, printer.id)
        assert len(rows) == 1  # one row per (printer, code), forever
        assert rows[0].count == 2
        assert rows[0].first_seen == first_seen  # the first sighting is never rewritten
        assert rows[0].last_seen >= first_last_seen

    async def test_the_throttle_is_per_code_not_per_printer(self, db_session, printer_factory, sessions):
        """One code inside its window must not suppress another code's first sighting."""
        printer = await printer_factory()
        with _Harness(session_factory=sessions, catalog=_CATALOG):
            await main_module.on_printer_status_change(printer.id, _state([_hms(_UNKNOWN_A)], layer_num=1))
            _rewind_throttle(printer.id, _UNKNOWN_A.full_code, main_module._HMS_EVENT_WRITE_INTERVAL_S + 1)
            await main_module.on_printer_status_change(
                printer.id, _state([_hms(_UNKNOWN_A), _hms(_UNKNOWN_B)], layer_num=2)
            )

        rows = {r.full_code: r.count for r in await _rows(db_session, printer.id)}
        assert rows == {_UNKNOWN_A.full_code: 2, _UNKNOWN_B.full_code: 1}


@pytest.mark.asyncio
class TestTheWriteNeverBreaksTheStatusFlow:
    async def test_a_failing_write_is_logged_and_swallowed(self, db_session, printer_factory, sessions, caplog):
        """Invariant 10: forensics must never break the status callback. The push still
        completes and the catalogued code still notifies."""
        printer = await printer_factory()
        with (
            _Harness(session_factory=sessions, catalog=_CATALOG) as h,
            # Break the writer and ONLY the writer: main imports the model inside the
            # guarded block and nothing else in the callback touches it.
            patch("backend.app.models.hms_event.HMSEvent", object()),
            caplog.at_level(logging.WARNING, logger=_MAIN_LOGGER),
        ):
            await main_module.on_printer_status_change(printer.id, _state([_hms(_UNKNOWN_A), _hms(_KNOWN)]))

        assert await _rows(db_session, printer.id) == []
        assert any("vocabulary write failed" in m for m in caplog.messages)
        assert h.notify.on_printer_error.await_count == 1


@pytest.mark.asyncio
class TestRetentionPrune:
    async def test_a_stale_row_is_swept_and_a_live_one_is_kept(self, db_session, printer_factory):
        """The startup hygiene pass (main's lifespan calls this with its own session)."""
        printer = await printer_factory()
        now = datetime.utcnow()
        stale = HMSEvent(
            printer_id=printer.id,
            full_code=_UNKNOWN_A.full_code,
            attr=_UNKNOWN_A.attr,
            code=0x30051,
            severity=3,
            first_seen=now - timedelta(days=400),
            last_seen=now - timedelta(days=main_module._HMS_EVENT_RETENTION_DAYS + 1),
            count=7,
        )
        live = HMSEvent(
            printer_id=printer.id,
            full_code=_UNKNOWN_B.full_code,
            attr=_UNKNOWN_B.attr,
            code=0x30005,
            severity=3,
            first_seen=now - timedelta(days=400),
            last_seen=now - timedelta(days=main_module._HMS_EVENT_RETENTION_DAYS - 1),
            count=2,
        )
        db_session.add_all([stale, live])
        await db_session.commit()

        pruned = await main_module._prune_hms_events(db_session)

        assert pruned == 1
        assert [r.full_code for r in await _rows(db_session, printer.id)] == [_UNKNOWN_B.full_code]

    async def test_an_empty_table_prunes_nothing(self, db_session):
        assert await main_module._prune_hms_events(db_session) == 0


# --- migration ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _force_sqlite_dialect(monkeypatch):
    """The migration branches on the dialect; pin the SQLite arm for this file."""
    from backend.app.core import database as database_module

    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


@pytest.mark.asyncio
class TestMigration:
    """The pre-existing-DB path: ``create_all`` never runs for a live install, so the
    raw DDL in ``run_migrations`` is what actually builds this table in production."""

    async def _migrated_engine(self):
        import backend.app.models  # noqa: F401 — register the mapped models
        from backend.app.core.database import Base, run_migrations

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Drop the create_all table so the migration's own DDL is exercised.
            await conn.execute(text("DROP TABLE hms_event"))
            await run_migrations(conn)
            await run_migrations(conn)  # second pass must be a no-op
        return engine

    async def test_running_it_twice_leaves_one_table_and_one_unique_index(self):
        engine = await self._migrated_engine()
        try:
            async with engine.connect() as conn:
                tables = (
                    (await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='hms_event'")))
                    .scalars()
                    .all()
                )
                indexes = (await conn.execute(text("PRAGMA index_list(hms_event)"))).fetchall()
            assert tables == ["hms_event"]
            unique = [i[1] for i in indexes if i[2]]
            assert unique == ["ux_hms_event_printer_code"]
        finally:
            await engine.dispose()

    async def test_the_natural_key_is_enforced(self):
        """One row per (printer, code): a bypassing second insert must die, so no code
        can ever hold two counters."""
        from sqlalchemy.exc import IntegrityError

        engine = await self._migrated_engine()
        try:
            maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            now = datetime.utcnow()

            def _row() -> HMSEvent:
                return HMSEvent(
                    printer_id=1,
                    full_code=_UNKNOWN_A.full_code,
                    attr=_UNKNOWN_A.attr,
                    code=0x30051,
                    severity=3,
                    first_seen=now,
                    last_seen=now,
                    count=1,
                )

            async with maker() as session:
                session.add(_row())
                await session.commit()
            async with maker() as session:
                session.add(_row())
                with pytest.raises(IntegrityError):
                    await session.commit()
            async with maker() as session:
                assert (await session.execute(select(func.count(HMSEvent.id)))).scalar_one() == 1
        finally:
            await engine.dispose()
