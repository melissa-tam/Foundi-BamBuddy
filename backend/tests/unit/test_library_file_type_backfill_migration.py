"""The library_files.file_type backfill migration (#1600).

The upload, ZIP-extract and in-process ingest paths stored `file_type='3mf'`
for sliced `.gcode.3mf` outputs while the external-folder scan stored
`file_type='gcode.3mf'` — one on-disk file family split across two values by
how it was ingested. `classify_file_type` is canonical now; this migration
backfills the legacy `3mf` rows. Dialect-neutral.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from backend.app.core.database import run_migrations
from backend.tests._fixtures.db import create_memory_engine

pytestmark = pytest.mark.usefixtures("force_sqlite_dialect")


@pytest.fixture
async def engine():
    eng = await create_memory_engine()
    yield eng
    await eng.dispose()


async def _insert_file(conn, *, file_id: int, filename: str, file_type: str) -> None:
    """Insert a minimal LibraryFile row; only the columns the migration
    touches matter."""
    await conn.execute(
        text(
            "INSERT INTO library_files "
            "(id, filename, file_path, file_type, file_size, is_external, print_count) "
            "VALUES (:id, :filename, :path, :ftype, 0, 0, 0)"
        ),
        {
            "id": file_id,
            "filename": filename,
            "path": f"/lib/{file_id}",
            "ftype": file_type,
        },
    )


@pytest.mark.asyncio
async def test_backfill_flips_only_legacy_gcode_3mf_rows(engine):
    """Rows with `file_type='3mf'` whose filename ends in `.gcode.3mf` get
    upgraded to `gcode.3mf`. Everything else stays put."""
    async with engine.begin() as conn:
        await _insert_file(conn, file_id=1, filename="sliced.gcode.3mf", file_type="3mf")
        await _insert_file(conn, file_id=2, filename="UPPER.GCODE.3MF", file_type="3mf")
        await _insert_file(conn, file_id=3, filename="model.3mf", file_type="3mf")  # not sliced
        await _insert_file(conn, file_id=4, filename="model.gcode", file_type="gcode")
        await _insert_file(conn, file_id=5, filename="model.stl", file_type="stl")
        await _insert_file(conn, file_id=6, filename="already.gcode.3mf", file_type="gcode.3mf")

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        rows = dict((await conn.execute(text("SELECT id, file_type FROM library_files ORDER BY id"))).fetchall())

    assert rows[1] == "gcode.3mf", "lowercase .gcode.3mf must be backfilled"
    assert rows[2] == "gcode.3mf", "uppercase .GCODE.3MF must be backfilled (LOWER(filename) in migration)"
    assert rows[3] == "3mf", "plain .3mf stays at `3mf` — not a sliced output"
    assert rows[4] == "gcode", "raw .gcode is untouched"
    assert rows[5] == "stl", "stl is untouched"
    assert rows[6] == "gcode.3mf", "rows already at canonical pass through"


@pytest.mark.asyncio
async def test_backfill_is_idempotent(engine):
    """Every boot re-runs the migration set; a second pass on already-
    backfilled rows must be a no-op."""
    async with engine.begin() as conn:
        await _insert_file(conn, file_id=1, filename="sliced.gcode.3mf", file_type="3mf")

    async with engine.begin() as conn:
        await run_migrations(conn)
    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT file_type FROM library_files WHERE id = 1"))
        assert result.scalar() == "gcode.3mf"


@pytest.mark.asyncio
async def test_backfill_leaves_unrelated_3mf_rows_alone(engine):
    """A row whose filename happens to contain `.gcode.3mf` as a substring
    but doesn't END with it (e.g. a `.bak` of a sliced output) is not a
    sliced output — must NOT be backfilled."""
    async with engine.begin() as conn:
        await _insert_file(conn, file_id=1, filename="sliced.gcode.3mf.bak", file_type="3mf")

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT file_type FROM library_files WHERE id = 1"))
        # The LIKE predicate is '%.gcode.3mf', so a trailing .bak does not match and
        # the row keeps `3mf` — while classify_file_type would call a fresh ingest of
        # it `bak`. The migration fixes the dominant class, it does not chase every
        # shape.
        assert result.scalar() == "3mf"
