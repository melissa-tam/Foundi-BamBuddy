#!/usr/bin/env python3
"""Read-only report of the foreign-replay repair, run against a live ``bambuddy.db``.

Prints what ``repair_foreign_replay_20260925`` WOULD do: the counts, then one line per action
and per skip, each with its before-values. That is the same text the migration logs row by row
when it applies. Run it on the farm PC right before the /ship that carries the migration, and
compare it against the log-evidenced damage.

Usage (the embedded Python has SQLAlchemy; nothing from the app is imported)::

    python foreign_replay_report.py <path-to-live-bambuddy.db> [--now 2026-09-25T12:00:00] [--apply-on-copy]

``--apply-on-copy`` adds a REHEARSAL: the plan is applied to the temporary copy inside a
transaction that is always rolled back, then planned again. That exercises the guarded apply,
the ``archive_fts`` triggers and every constraint against the real rows, which the plan alone
cannot. The migration's savepoint would roll back and retry at every boot if any of those
failed. The rehearsal exits non-zero when the apply fails or leaves actions behind.

The repair module is loaded BY FILE PATH: first ``foreign_replay_repair.py`` beside this script
(the scp'd layout), then ``backend/app/services/foreign_replay_repair.py`` in a checkout.

**The live database is never written.** It is opened read-only through a ``mode=ro`` URI with a
busy timeout, never ``immutable=1``: the farm writes to it while this runs, and ``immutable``
turns off locking and change detection, so a concurrent commit could tear the read. Its content
goes to a temporary copy in one step through the SQLite backup API, which reads through the
connection and so includes pages still in the WAL (a raw file copy would lose them). The source
is closed at once. The plan runs on the COPY. Every connection is closed and the engine disposed
in a ``finally``, and then the copy is deleted. If the copy cannot be deleted the script says
so and exits non-zero, because production data must not linger.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

from sqlalchemy import Connection, Engine, create_engine

_MODULE_NAME = "foreign_replay_repair"
_SCRIPT_DIR = Path(__file__).resolve().parent
_MODULE_CANDIDATES = (
    _SCRIPT_DIR / f"{_MODULE_NAME}.py",
    _SCRIPT_DIR.parent / "app" / "services" / f"{_MODULE_NAME}.py",
)
_BUSY_TIMEOUT_S = 30


class CopyNotDeleted(RuntimeError):
    """The temporary copy of the live database survived the cleanup."""


def load_repair_module() -> ModuleType:
    """Import the standalone repair module by path, with no package and no app on ``sys.path``.

    The module registers in ``sys.modules`` before it executes, because ``dataclasses`` resolves
    the module's annotations through it.
    """
    path = next((candidate for candidate in _MODULE_CANDIDATES if candidate.is_file()), None)
    if path is None:
        raise FileNotFoundError(f"{_MODULE_NAME}.py not found at any of: {', '.join(map(str, _MODULE_CANDIDATES))}")
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _rehearse(repair: ModuleType, conn: Connection, plan: object, now: datetime) -> tuple[str, bool]:
    """Apply ``plan`` to the COPY, plan again, and roll back. Returns the section and whether it passed."""
    header = "\nREHEARSAL (applied to the temporary copy, then rolled back)\n"
    try:
        repair.apply_foreign_replay_repair(conn, plan)
        replan = repair.plan_foreign_replay_repair(conn, now=now)
    except Exception as exc:  # noqa: BLE001 -- reporting every failure is the rehearsal's whole job
        return f"{header}  FAILED: {type(exc).__name__}: {ascii(str(exc))[1:-1]}\n", False
    finally:
        conn.rollback()
    left = len(replan.actions)
    verdict = "OK" if left == 0 else "FAILED: the apply left actions behind"
    return f"{header}  {verdict}: applied {len(plan.actions)} action(s); a second plan finds {left}\n", left == 0


def build_report(source: Path, *, now: datetime, apply_on_copy: bool = False) -> tuple[str, bool]:
    """Plan the repair on a backup-API copy of ``source``. Returns the report and whether it passed.

    Without ``apply_on_copy`` a report always passes: it only describes.
    """
    repair = load_repair_module()
    if not source.is_file():
        raise FileNotFoundError(f"no database at {source}")
    workdir = Path(tempfile.mkdtemp(prefix="foreign-replay-report-"))
    copy_path = workdir / "bambuddy-copy.db"
    src: sqlite3.Connection | None = None
    dst: sqlite3.Connection | None = None
    engine: Engine | None = None
    try:
        src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_S)
        dst = sqlite3.connect(copy_path)
        src.backup(dst, pages=-1)
        dst.close()
        dst = None
        src.close()
        src = None
        engine = create_engine(f"sqlite:///{copy_path.as_posix()}")
        with engine.connect() as conn:
            plan = repair.plan_foreign_replay_repair(conn, now=now)
            report, passed = repair.format_report(plan), True
            if apply_on_copy:
                section, passed = _rehearse(repair, conn, plan, now)
                report += section
        return report, passed
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()
        if engine is not None:
            engine.dispose()
        shutil.rmtree(workdir, ignore_errors=True)
        if workdir.exists():
            raise CopyNotDeleted(f"the temporary copy under {workdir} could not be deleted; delete it by hand")


def _parse_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only report of the foreign-replay repair.")
    parser.add_argument("database", type=Path, help="path to the live bambuddy.db (opened read-only)")
    parser.add_argument("--now", help="ISO timestamp R-dup stamps on archives it closes unknown (default: now, UTC)")
    parser.add_argument(
        "--apply-on-copy",
        action="store_true",
        help="also rehearse the apply on the temporary copy (rolled back; the live file is never written)",
    )
    args = parser.parse_args(argv)
    # The report is ASCII by construction; this only keeps an odd path in an error printable.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    report, passed = build_report(args.database, now=_parse_now(args.now), apply_on_copy=args.apply_on_copy)
    sys.stdout.write(report)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
