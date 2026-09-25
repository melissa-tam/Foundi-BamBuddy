"""The one-time foreign-replay repair: its rules, its guarded apply, its migration and its report.

Each seeded shape is one the production evidence showed: a leaked archive replayed days later,
the misbinding that closed the CURRENT print's archive mid-run, and a replay written by another
printer. Each shape the rules must leave alone is seeded too: a run recorded ``cancelled``, a
retry chain's genuine rows, a foreign print, and weight-locked, spent or archived spools.

Attribution is by RUN, never by archive. The retry-chain case is the one an archive-keyed rule
gets wrong: the parent's rows sit on the same archive as the child's, and only the run windows
tell them apart.
"""

from __future__ import annotations

import ast
import logging
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Connection, Row, delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from backend.app.core.database import Base, run_migrations
from backend.app.services import foreign_replay_repair as frr
from backend.tests._fixtures.db import create_memory_engine, import_all_models

_BACKEND = Path(__file__).resolve().parents[3]
_MODULE_PATH = _BACKEND / "app" / "services" / "foreign_replay_repair.py"
_RUNNER_PATH = _BACKEND / "scripts" / "foreign_replay_report.py"
_MARKER = "repair_foreign_replay_20260925"

T0 = datetime(2026, 9, 20, 6, 0, 0)
NOW = datetime(2026, 9, 25, 12, 0, 0)
S = timedelta(seconds=1)
M = timedelta(minutes=1)
H = timedelta(hours=1)
D = timedelta(days=1)

Shape = Callable[["_Seed"], None]


def _printer_name(printer_id: int) -> str:
    # One printer carries a non-ASCII name: the report must still print on a cp1252 console.
    return "012-Drucker-ü" if printer_id == 12 else f"{printer_id:03d}-H2S"


@dataclass
class _Seed:
    """Rows to insert, in order, through the REAL tables (``Base.metadata``)."""

    rows: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def printer(self, printer_id: int) -> None:
        self.rows.append(
            (
                "printers",
                {
                    "id": printer_id,
                    "name": _printer_name(printer_id),
                    "serial_number": f"SN{printer_id:06d}",
                    "ip_address": f"192.0.2.{printer_id}",
                    "access_code": "00000000",
                },
            )
        )

    def archive(
        self,
        archive_id: int,
        printer_id: int,
        *,
        status: str,
        subtask: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        failure_reason: str | None = None,
    ) -> None:
        self.rows.append(
            (
                "print_archives",
                {
                    "id": archive_id,
                    "printer_id": printer_id,
                    "filename": f"{archive_id}.3mf",
                    "file_path": f"archive/{archive_id}.3mf",
                    "file_size": 1,
                    "print_name": "1d1054d9f48447c8ba850943eded4852",
                    "status": status,
                    "subtask_id": subtask,
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "filament_used_grams": 120.0,
                    "cost": 3.0,
                    "failure_reason": failure_reason,
                },
            )
        )

    def unit(
        self,
        unit_id: int,
        printer_id: int,
        *,
        status: str,
        archive_id: int | None = None,
        subtask: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        retry_of_id: int | None = None,
        stop_source: str | None = None,
    ) -> None:
        self.rows.append(
            (
                "print_queue",
                {
                    "id": unit_id,
                    "printer_id": printer_id,
                    "archive_id": archive_id,
                    "dispatch_subtask_id": subtask,
                    "status": status,
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "retry_of_id": retry_of_id,
                    "stop_source": stop_source,
                },
            )
        )

    def log(
        self,
        entry_id: int,
        archive_id: int,
        printer_id: int,
        *,
        status: str,
        created_at: datetime,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        grams: float | None = None,
        cost: float | None = None,
        failure_reason: str | None = None,
    ) -> None:
        duration = int((completed_at - started_at).total_seconds()) if started_at and completed_at else None
        self.rows.append(
            (
                "print_log_entries",
                {
                    "id": entry_id,
                    "archive_id": archive_id,
                    "printer_id": printer_id,
                    "printer_name": _printer_name(printer_id),
                    "status": status,
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "duration_seconds": duration,
                    "filament_used_grams": grams,
                    "cost": cost,
                    "failure_reason": failure_reason,
                    "created_at": created_at,
                },
            )
        )

    def spool(
        self,
        spool_id: int,
        *,
        weight_used: float,
        baseline: float = 0.0,
        locked: bool = False,
        spent_at: datetime | None = None,
        archived_at: datetime | None = None,
    ) -> None:
        self.rows.append(
            (
                "spool",
                {
                    "id": spool_id,
                    "material": "PETG",
                    "weight_used": weight_used,
                    "weight_used_baseline": baseline,
                    "weight_locked": locked,
                    "spent_at": spent_at,
                    "archived_at": archived_at,
                },
            )
        )

    def charge(
        self,
        charge_id: int,
        spool_id: int,
        archive_id: int | None,
        printer_id: int,
        *,
        grams: float,
        status: str,
        created_at: datetime,
        cost: float | None = None,
    ) -> None:
        self.rows.append(
            (
                "spool_usage_history",
                {
                    "id": charge_id,
                    "spool_id": spool_id,
                    "archive_id": archive_id,
                    "printer_id": printer_id,
                    "print_name": "1d1054d9f48447c8ba850943eded4852",
                    "weight_used": grams,
                    "cost": cost,
                    "status": status,
                    "created_at": created_at,
                },
            )
        )

    async def write(self, conn: AsyncConnection) -> None:
        for table, values in self.rows:
            await conn.execute(insert(Base.metadata.tables[table]).values(**values))


# --- the shapes ---------------------------------------------------------------------------------------


def _shape_leaked(s: _Seed) -> None:
    """(a) The real completion at T0 could not find its archive (restart), charged through the
    donor with no archive and wrote no print-log row. Two days later a reconnect replayed it."""
    s.printer(1)
    s.archive(
        101,
        1,
        status="cancelled",
        subtask="1001",
        started_at=T0 - 4 * H,
        completed_at=T0 + 2 * D,
        failure_reason="reconcile",
    )
    s.unit(201, 1, status="completed", archive_id=101, subtask="1001", started_at=T0 - 4 * H, completed_at=T0)
    s.spool(301, weight_used=673.0)
    s.charge(9001, 301, None, 1, grams=118.0, status="completed", created_at=T0 + 5 * S, cost=2.95)  # the donor
    s.log(
        501,
        101,
        1,
        status="cancelled",
        created_at=T0 + 2 * D,
        started_at=T0 - 4 * H,
        completed_at=T0 + 2 * D,
        grams=55.0,
        cost=1.1,
        failure_reason="reconcile",
    )
    s.charge(601, 301, 101, 1, grams=55.0, status="cancelled", created_at=T0 + 2 * D + S)


def _shape_misbinding(s: _Seed) -> None:
    """(b) A stale archive's replay at T closed the CURRENT print's archive mid-run; that print
    really completed at T+3h and could no longer find its (already cancelled) archive."""
    s.printer(2)
    s.archive(111, 2, status="cancelled", subtask="1011", started_at=T0 - H, completed_at=T0)
    s.unit(
        211, 2, status="completed", archive_id=111, subtask="1011", started_at=T0 - H - 30 * S, completed_at=T0 + 3 * H
    )
    s.spool(302, weight_used=400.0)
    s.log(511, 111, 2, status="cancelled", created_at=T0, started_at=T0 - H, completed_at=T0, grams=20.0, cost=0.5)
    s.charge(611, 302, 111, 2, grams=80.0, status="cancelled", created_at=T0 + S)


def _shape_unobserved(s: _Seed) -> None:
    """(c) The downtime reconcile genuinely never saw how this print ended. The replay at T0 WAS
    the run's terminal: it matched the still-printing unit and stamped its ``completed_at``, so its
    rows sit inside the run's own window."""
    s.printer(3)
    s.archive(121, 3, status="cancelled", subtask="1021", started_at=T0 - 2 * H, completed_at=T0)
    s.unit(221, 3, status="cancelled", archive_id=121, subtask="1021", started_at=T0 - 2 * H, completed_at=T0)
    s.spool(303, weight_used=250.0)
    s.log(521, 121, 3, status="cancelled", created_at=T0 + 2 * S, started_at=T0 - 2 * H, completed_at=T0)
    s.charge(621, 303, 121, 3, grams=30.0, status="cancelled", created_at=T0 + S)


def _shape_failed(s: _Seed) -> None:
    """(d) A failed run with its own genuine rows at its terminal, replayed a day later."""
    s.printer(4)
    s.archive(131, 4, status="cancelled", subtask="1031", started_at=T0 - 3 * H, completed_at=T0 + D)
    s.unit(231, 4, status="failed", archive_id=131, subtask="1031", started_at=T0 - 3 * H, completed_at=T0)
    s.spool(304, weight_used=365.0)
    s.log(
        531,
        131,
        4,
        status="failed",
        created_at=T0 + 20 * S,
        started_at=T0 - 3 * H,
        completed_at=T0,
        grams=25.0,
        failure_reason="clog",
    )
    s.charge(631, 304, 131, 4, grams=25.0, status="failed", created_at=T0 + 15 * S)
    s.log(532, 131, 4, status="cancelled", created_at=T0 + D, started_at=T0 - 3 * H, completed_at=T0 + D)
    s.charge(632, 304, 131, 4, grams=40.0, status="cancelled", created_at=T0 + D + S)


def _shape_retry_chain(s: _Seed) -> None:
    """(e) A retry carries its parent's archive (``requeue_fields``), and the child's print start
    re-stamped it. The parent's genuine rows at ITS terminal share the archive with the child's
    replay, and include a stop-word charge that an archive-keyed rule would reverse."""
    s.printer(5)
    s.archive(141, 5, status="cancelled", subtask="1042", started_at=T0 + 10 * M, completed_at=T0 + 2 * D)
    s.unit(240, 5, status="failed", archive_id=141, subtask="1041", started_at=T0 - 3 * H, completed_at=T0)
    s.unit(
        241,
        5,
        status="completed",
        archive_id=141,
        subtask="1042",
        started_at=T0 + 10 * M,
        completed_at=T0 + 3 * H,
        retry_of_id=240,
    )
    s.spool(305, weight_used=600.0)
    s.log(541, 141, 5, status="failed", created_at=T0 + 20 * S, started_at=T0 - 3 * H, completed_at=T0)
    s.charge(641, 305, 141, 5, grams=25.0, status="failed", created_at=T0 + 15 * S)
    s.charge(642, 305, 141, 5, grams=10.0, status="cancelled", created_at=T0 + 30 * S)
    s.log(542, 141, 5, status="cancelled", created_at=T0 + 2 * D, started_at=T0 + 10 * M, completed_at=T0 + 2 * D)
    s.charge(643, 305, 141, 5, grams=60.0, status="cancelled", created_at=T0 + 2 * D + S)


def _shape_retired_spools(s: _Seed) -> None:
    """(f) Replay charges on spools whose weight the repair must not move, beside one it may."""
    s.printer(6)
    s.archive(151, 6, status="cancelled", subtask="1051", started_at=T0 - 4 * H, completed_at=T0 + 2 * D)
    s.unit(251, 6, status="completed", archive_id=151, subtask="1051", started_at=T0 - 4 * H, completed_at=T0)
    s.spool(306, weight_used=300.0, locked=True)
    s.spool(307, weight_used=300.0, spent_at=T0 + D)
    s.spool(308, weight_used=300.0, archived_at=T0 + D)
    s.spool(309, weight_used=300.0)
    s.log(551, 151, 6, status="cancelled", created_at=T0 + 2 * D, started_at=T0 - 4 * H, completed_at=T0 + 2 * D)
    for charge_id, spool_id in ((651, 306), (652, 307), (653, 308), (654, 309)):
        s.charge(charge_id, spool_id, 151, 6, grams=50.0, status="cancelled", created_at=T0 + 2 * D + S)


def _shape_printing_duplicates(s: _Seed) -> None:
    """(g) Printer 7 carries five ``printing`` archives; printer 8 carries one."""
    s.printer(7)
    s.printer(8)
    s.archive(160, 7, status="printing", subtask="1060", started_at=T0 - 2 * D)
    s.unit(
        260,
        7,
        status="completed",
        archive_id=160,
        subtask="1060",
        started_at=T0 - 2 * D,
        completed_at=T0 - 2 * D + 4 * H,
    )
    s.archive(161, 7, status="printing", subtask="1061", started_at=T0 - D)
    s.unit(261, 7, status="printing", archive_id=161, subtask="1061", started_at=T0 - D)
    s.archive(162, 7, status="printing", started_at=T0 - 12 * H)
    s.archive(164, 7, status="printing", subtask="1064", started_at=T0 - 6 * H)
    s.unit(264, 7, status="cancelled", archive_id=164, subtask="1064", started_at=T0 - 6 * H, completed_at=T0 - 5 * H)
    s.archive(163, 7, status="printing", subtask="1063", started_at=T0)
    s.archive(170, 8, status="printing", subtask="1070", started_at=T0 - 3 * D)


def _shape_cross_printer(s: _Seed) -> None:
    """(k) Printer 10's replay closed printer 9's archive; printer 9's own replay did too.
    (k2) Printer 12's replay is the only row left describing printer 11's run."""
    for printer_id in (9, 10, 11, 12):
        s.printer(printer_id)
    s.archive(181, 9, status="cancelled", subtask="1081", started_at=T0 - 4 * H, completed_at=T0 + D)
    s.unit(281, 9, status="completed", archive_id=181, subtask="1081", started_at=T0 - 4 * H, completed_at=T0)
    s.spool(310, weight_used=500.0)
    s.log(581, 181, 10, status="cancelled", created_at=T0 + D, started_at=T0 - 4 * H, completed_at=T0 + D)
    s.log(582, 181, 9, status="cancelled", created_at=T0 + D + 2 * S, started_at=T0 - 4 * H, completed_at=T0 + D)
    s.charge(681, 310, 181, 10, grams=70.0, status="cancelled", created_at=T0 + D + S)
    s.archive(191, 11, status="cancelled", subtask="1091", started_at=T0 - 4 * H, completed_at=T0 + D)
    s.unit(291, 11, status="completed", archive_id=191, subtask="1091", started_at=T0 - 4 * H, completed_at=T0)
    s.log(591, 191, 12, status="cancelled", created_at=T0 + D, started_at=T0 - 4 * H, completed_at=T0 + D)


def _shape_foreign(s: _Seed) -> None:
    """(l) An operator reprinted a farm archive from the screen (subtask "0") and stopped it; and
    a print the farm never dispatched at all. Neither has a farm run to repair against."""
    s.printer(13)
    s.archive(195, 13, status="cancelled", subtask="0", started_at=T0 + D, completed_at=T0 + D + 2 * H)
    s.unit(295, 13, status="completed", archive_id=195, subtask="1095", started_at=T0 - 4 * H, completed_at=T0)
    s.spool(311, weight_used=200.0)
    s.charge(695, 311, 195, 13, grams=15.0, status="cancelled", created_at=T0 + D + 2 * H)
    s.archive(196, 13, status="cancelled", started_at=T0 - 2 * D, completed_at=T0 - 2 * D + H)
    s.log(596, 196, 13, status="cancelled", created_at=T0 - 2 * D + H)
    s.charge(696, 311, 196, 13, grams=5.0, status="cancelled", created_at=T0 - 2 * D + H)


def _shape_pre_0919(s: _Seed) -> None:
    """(m) A replay before 2026-09-19 kept the client's raw ``aborted`` on all three records."""
    s.printer(14)
    s.archive(197, 14, status="aborted", subtask="1097", started_at=T0 - 4 * H, completed_at=T0 + D)
    s.unit(297, 14, status="completed", archive_id=197, subtask="1097", started_at=T0 - 4 * H, completed_at=T0)
    s.spool(312, weight_used=400.0)
    s.log(597, 197, 14, status="aborted", created_at=T0 + D, started_at=T0 - 4 * H, completed_at=T0 + D)
    s.charge(699, 312, 197, 14, grams=25.0, status="aborted", created_at=T0 + D + S)


def _shape_true_status_beside_genuine(s: _Seed) -> None:
    """(n) The true-status branch: the printer still showed that job's FINISH, so the replay wrote
    ``completed`` a day after the real terminal, which had already charged and logged it."""
    s.printer(15)
    s.archive(198, 15, status="completed", subtask="1098", started_at=T0 - 4 * H, completed_at=T0 + D)
    s.unit(298, 15, status="completed", archive_id=198, subtask="1098", started_at=T0 - 4 * H, completed_at=T0)
    s.spool(313, weight_used=500.0)
    s.charge(698, 313, 198, 15, grams=110.0, status="completed", created_at=T0 + 5 * S)
    s.log(599, 198, 15, status="completed", created_at=T0 + 10 * S, started_at=T0 - 4 * H, completed_at=T0, grams=110.0)
    s.charge(690, 313, 198, 15, grams=110.0, status="completed", created_at=T0 + D)
    s.log(598, 198, 15, status="completed", created_at=T0 + D, started_at=T0 - 4 * H, completed_at=T0 + D, grams=110.0)


def _shape_true_status_after_archiveless_terminal(s: _Seed) -> None:
    """(o) The same branch after a real terminal that could not find the archive: the replay's
    ``completed`` row is the run's only print-log record, with the replay's time and grams."""
    s.printer(16)
    s.archive(199, 16, status="completed", subtask="1099", started_at=T0 - 4 * H, completed_at=T0 + D)
    s.unit(299, 16, status="completed", archive_id=199, subtask="1099", started_at=T0 - 4 * H, completed_at=T0)
    s.spool(314, weight_used=300.0)
    s.charge(9002, 314, None, 16, grams=118.0, status="completed", created_at=T0 + 5 * S, cost=2.9)  # the donor
    s.log(
        590,
        199,
        16,
        status="completed",
        created_at=T0 + D,
        started_at=T0 - 4 * H,
        completed_at=T0 + D,
        grams=90.0,
        cost=2.2,
    )
    s.charge(691, 314, 199, 16, grams=95.0, status="completed", created_at=T0 + D + S)


def _shape_run_still_printing(s: _Seed) -> None:
    """(p) A replay closed the CURRENT print's archive mid-run, and that print is still running:
    its terminal, the evidence the rules judge by, has not happened yet."""
    s.printer(17)
    s.archive(200, 17, status="cancelled", subtask="1100", started_at=T0 - H, completed_at=T0)
    s.unit(300, 17, status="printing", archive_id=200, subtask="1100", started_at=T0 - H - 30 * S)
    s.spool(315, weight_used=250.0)
    s.log(594, 200, 17, status="cancelled", created_at=T0, started_at=T0 - H, completed_at=T0)
    s.charge(692, 315, 200, 17, grams=35.0, status="cancelled", created_at=T0 + S)


def _shape_cancelled_run_replayed_later(s: _Seed) -> None:
    """(q) Printer 10 / archive 2318 in production: the real terminal arrived CANCELLED and charged
    then; the restart replay 42 h later, finding the unit already ended, resolved FOREIGN and
    charged again."""
    s.printer(18)
    s.archive(210, 18, status="cancelled", subtask="1110", started_at=T0 - 3 * H, completed_at=T0 + 42 * H)
    s.unit(310, 18, status="cancelled", archive_id=210, subtask="1110", started_at=T0 - 3 * H, completed_at=T0)
    s.spool(316, weight_used=400.0)
    s.charge(693, 316, 210, 18, grams=20.0, status="cancelled", created_at=T0 + 5 * S)
    s.charge(694, 316, 210, 18, grams=133.7, status="cancelled", created_at=T0 + 42 * H)
    s.log(
        589,
        210,
        18,
        status="cancelled",
        created_at=T0 + 42 * H,
        started_at=T0 - 3 * H,
        completed_at=T0 + 42 * H,
        grams=133.7,
        cost=3.3,
    )


def _shape_donor_grams(s: _Seed) -> None:
    """(r) A cancelled run whose real terminal charged two spools through the donor. The run's
    grams are those two charges, and not another printer's, nor a later one on this printer."""
    s.printer(19)
    s.printer(20)
    s.archive(211, 19, status="cancelled", subtask="1111", started_at=T0 - 2 * H, completed_at=T0 + 42 * H)
    s.unit(311, 19, status="cancelled", archive_id=211, subtask="1111", started_at=T0 - 2 * H, completed_at=T0)
    s.spool(317, weight_used=500.0)
    s.spool(318, weight_used=300.0)
    s.charge(9003, 317, None, 19, grams=22.5, status="cancelled", created_at=T0 + 5 * S, cost=0.55)
    s.charge(9004, 318, None, 19, grams=10.0, status="cancelled", created_at=T0 + 6 * S, cost=0.25)
    s.charge(9005, 317, None, 20, grams=99.0, status="completed", created_at=T0 + 5 * S, cost=2.4)
    s.charge(9006, 317, None, 19, grams=7.0, status="completed", created_at=T0 + H, cost=0.2)
    s.log(
        593,
        211,
        19,
        status="cancelled",
        created_at=T0 + 42 * H,
        started_at=T0 - 2 * H,
        completed_at=T0 + 42 * H,
        grams=150.0,
        cost=3.5,
    )
    s.charge(686, 317, 211, 19, grams=45.0, status="cancelled", created_at=T0 + 42 * H + S)


def _shape_queue_page_stops(s: _Seed) -> None:
    """(s) Two units stopped from the queue page, whose route stamps ``completed_at`` before the
    terminal. Printer 21 was offline: the stop never arrived and the print's real terminal came
    two hours later. Printer 22's terminal followed the stop within seconds, then a replay came."""
    s.printer(21)
    s.printer(22)
    s.archive(212, 21, status="completed", subtask="1112", started_at=T0 - H, completed_at=T0 + 2 * H)
    s.unit(
        312,
        21,
        status="cancelled",
        archive_id=212,
        subtask="1112",
        started_at=T0 - H,
        completed_at=T0,
        stop_source="operator_ui",
    )
    s.spool(319, weight_used=300.0)
    s.charge(687, 319, 212, 21, grams=60.0, status="completed", created_at=T0 + 2 * H)
    s.log(588, 212, 21, status="completed", created_at=T0 + 2 * H, started_at=T0 - H, completed_at=T0 + 2 * H)
    s.archive(213, 22, status="cancelled", subtask="1113", started_at=T0 - H, completed_at=T0 + 42 * H)
    s.unit(
        313,
        22,
        status="cancelled",
        archive_id=213,
        subtask="1113",
        started_at=T0 - H,
        completed_at=T0,
        stop_source="operator_ui",
    )
    s.spool(320, weight_used=250.0)
    s.charge(9007, 320, None, 22, grams=12.0, status="cancelled", created_at=T0 + 20 * S)
    s.charge(688, 320, 213, 22, grams=30.0, status="cancelled", created_at=T0 + 42 * H)


ALL_SHAPES: tuple[Shape, ...] = (
    _shape_leaked,
    _shape_misbinding,
    _shape_unobserved,
    _shape_failed,
    _shape_retry_chain,
    _shape_retired_spools,
    _shape_printing_duplicates,
    _shape_cross_printer,
    _shape_foreign,
    _shape_pre_0919,
    _shape_true_status_beside_genuine,
    _shape_true_status_after_archiveless_terminal,
    _shape_run_still_printing,
    _shape_cancelled_run_replayed_later,
    _shape_donor_grams,
    _shape_queue_page_stops,
)


# --- harness --------------------------------------------------------------------------------------------


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """The app's own schema (``create_memory_engine``). The repair takes a SYNC ``Connection``, which
    ``run_sync`` hands it — the same bridge the migration uses."""
    eng = await create_memory_engine()
    async with eng.begin() as conn:
        # The pre-repair shapes hold several printing archives per printer; production builds this index after R-dup.
        await conn.exec_driver_sql("DROP INDEX IF EXISTS ux_print_archives_live_printer")
    yield eng
    await eng.dispose()


async def _seed(engine: AsyncEngine, *shapes: Shape) -> None:
    seed = _Seed()
    for shape in shapes:
        shape(seed)
    async with engine.begin() as conn:
        await seed.write(conn)


async def _plan(engine: AsyncEngine, now: datetime = NOW) -> frr.RepairPlan:
    async with engine.connect() as conn:
        return await conn.run_sync(frr.plan_foreign_replay_repair, now=now)


async def _repair(engine: AsyncEngine) -> frr.RepairPlan:
    plan = await _plan(engine)
    async with engine.begin() as conn:
        await conn.run_sync(frr.apply_foreign_replay_repair, plan)
    return plan


async def _row(engine: AsyncEngine, table: str, row_id: int) -> Row | None:
    tab = Base.metadata.tables[table]
    async with engine.connect() as conn:
        return (await conn.execute(select(tab).where(tab.c.id == row_id))).one_or_none()


def _ids(plan: frr.RepairPlan, kind: type) -> set[int]:
    """The subject row id of every action (or skip) of one kind."""
    subject = {
        frr.ClosePrintingDuplicate: "archive_id",
        frr.RestoreArchiveOutcome: "archive_id",
        frr.RewritePrintLogEntry: "entry_id",
        frr.DropPrintLogEntry: "entry_id",
        frr.ReverseSpoolCharge: "usage_id",
        frr.SkipSpoolCharge: "usage_id",
        frr.SkipRow: "row_id",
    }[kind]
    return {getattr(item, subject) for item in (*plan.actions, *plan.skips) if isinstance(item, kind)}


def _skip_codes(plan: frr.RepairPlan) -> dict[tuple[str, int], str]:
    return {
        (
            "spool_usage_history" if isinstance(skip, frr.SkipSpoolCharge) else skip.table,
            skip.usage_id if isinstance(skip, frr.SkipSpoolCharge) else skip.row_id,
        ): skip.code
        for skip in plan.skips
    }


# --- the schema contract --------------------------------------------------------------------------------


def test_every_stub_column_exists_in_the_live_models() -> None:
    """The module names columns by hand (it cannot import the models); this is the drift pin."""
    import_all_models()
    missing = [
        f"{table}.{column}"
        for table, columns in frr.STUB_COLUMNS.items()
        for column in columns
        if column not in Base.metadata.tables[table].c
    ]
    assert not missing, f"the repair's table stubs name columns the models no longer have: {missing}"


@pytest.mark.parametrize("path", [_MODULE_PATH, _RUNNER_PATH], ids=["module", "runner"])
def test_imports_only_the_standard_library_and_sqlalchemy(path: Path) -> None:
    """Both files run BY PATH under the farm PC's embedded Python, where no app is importable."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None, f"relative import in {path.name}"
            roots.add(node.module.split(".")[0])
    assert "backend" not in roots
    assert roots <= set(sys.stdlib_module_names) | {"sqlalchemy"}, roots - set(sys.stdlib_module_names)


# --- the rules, per shape -------------------------------------------------------------------------------


async def test_leaked_archive_replayed_later_is_restored_and_its_phantom_charge_reversed(engine: AsyncEngine) -> None:
    """(a)"""
    await _seed(engine, _shape_leaked)
    plan = await _repair(engine)

    assert _ids(plan, frr.RestoreArchiveOutcome) == {101}
    assert _ids(plan, frr.RewritePrintLogEntry) == {501}
    assert _ids(plan, frr.ReverseSpoolCharge) == {601}
    assert len(plan.actions) == 3

    archive = await _row(engine, "print_archives", 101)
    assert (archive.status, archive.completed_at, archive.failure_reason) == ("completed", T0, None)
    entry = await _row(engine, "print_log_entries", 501)
    assert entry.status == "completed"
    assert entry.completed_at == T0
    assert entry.duration_seconds == 4 * 3600, "recomputed from the row's own start to the run's terminal"
    assert (entry.filament_used_grams, entry.cost) == (118.0, 2.95), "the real terminal's own donor charge"
    assert entry.failure_reason is None
    assert entry.created_at == T0 + 2 * D, "the write time is history; it is not rewritten"
    assert await _row(engine, "spool_usage_history", 601) is None
    assert (await _row(engine, "spool", 301)).weight_used == pytest.approx(618.0)
    assert await _row(engine, "spool_usage_history", 9001) is not None, "the real completion's donor charge stays"


async def test_misbinding_mid_run_is_attributed_to_the_run_whose_window_holds_it(engine: AsyncEngine) -> None:
    """(b) The replay row lands INSIDE the current run's window, hours before its terminal."""
    await _seed(engine, _shape_misbinding)
    plan = await _repair(engine)

    assert _ids(plan, frr.RestoreArchiveOutcome) == {111}
    assert _ids(plan, frr.ReverseSpoolCharge) == {611}
    archive = await _row(engine, "print_archives", 111)
    assert (archive.status, archive.completed_at) == ("completed", T0 + 3 * H)
    entry = await _row(engine, "print_log_entries", 511)
    assert (entry.status, entry.completed_at, entry.duration_seconds) == ("completed", T0 + 3 * H, 4 * 3600)
    assert await _row(engine, "spool_usage_history", 611) is None
    assert (await _row(engine, "spool", 302)).weight_used == pytest.approx(320.0)


async def test_a_genuinely_unobserved_outcome_is_the_runs_own_record(engine: AsyncEngine) -> None:
    """(c) The replay was the run's terminal, so its rows are the only record of the run."""
    await _seed(engine, _shape_unobserved)
    plan = await _repair(engine)

    assert plan.actions == ()
    assert plan.skips == ()
    tallies = dict(plan.tallies)
    assert tallies["charge: genuine: written at its run's own terminal"] == 1
    assert tallies["print-log: genuine: written at its run's own terminal"] == 1
    assert (await _row(engine, "print_archives", 121)).status == "cancelled"
    assert (await _row(engine, "print_log_entries", 521)).status == "cancelled"
    assert await _row(engine, "spool_usage_history", 621) is not None
    assert (await _row(engine, "spool", 303)).weight_used == pytest.approx(250.0)


async def test_a_cancelled_run_replayed_later_is_repaired_and_its_own_charge_kept(engine: AsyncEngine) -> None:
    """(q) The run recorded cancelled at its real terminal; the replay 42 h later is a replay."""
    await _seed(engine, _shape_cancelled_run_replayed_later)
    plan = await _repair(engine)

    assert _ids(plan, frr.ReverseSpoolCharge) == {694}
    assert await _row(engine, "spool_usage_history", 693) is not None, "the genuine charge at its terminal stays"
    assert await _row(engine, "spool_usage_history", 694) is None
    assert (await _row(engine, "spool", 316)).weight_used == pytest.approx(266.3)
    entry = await _row(engine, "print_log_entries", 589)
    assert (entry.status, entry.completed_at, entry.duration_seconds) == ("cancelled", T0, 3 * 3600)
    assert entry.filament_used_grams is None, "a cancelled run with no donor charge has no known grams"
    assert entry.cost == 3.3, "its cost stays as written"
    assert _ids(plan, frr.RestoreArchiveOutcome) == set(), "R-archive restores only completed/failed outcomes"
    assert (await _row(engine, "print_archives", 210)).status == "cancelled"


async def test_a_rewritten_row_takes_its_grams_from_the_real_terminals_donor_charges(engine: AsyncEngine) -> None:
    """(r) The two donor charges on the run's printer within its window, and nothing else."""
    await _seed(engine, _shape_donor_grams)
    plan = await _repair(engine)

    rewrite = next(a for a in plan.actions if isinstance(a, frr.RewritePrintLogEntry))
    assert rewrite.after.filament_used_grams == 32.5
    assert rewrite.after.cost == pytest.approx(0.8)
    entry = await _row(engine, "print_log_entries", 593)
    assert (entry.status, entry.completed_at, entry.filament_used_grams) == ("cancelled", T0, 32.5)
    assert _ids(plan, frr.ReverseSpoolCharge) == {686}
    for donor in (9003, 9004, 9005, 9006):
        assert await _row(engine, "spool_usage_history", donor) is not None, "donor charges are never touched"
    assert (await _plan(engine)).actions == (), "the rewrite is idempotent"


async def test_a_queue_page_stop_is_judged_only_once_its_terminal_is_seen(engine: AsyncEngine) -> None:
    """(s) The Stop route's ``completed_at`` is not a terminal. With no terminal written beside it
    (an offline printer), a later row may be the print's real end: listed, never reversed. With
    the terminal seen right after the stop, the premise holds and the replay is reversed."""
    await _seed(engine, _shape_queue_page_stops)
    plan = await _repair(engine)

    assert _skip_codes(plan) == {
        ("print_log_entries", 588): "stop_terminal_unseen",
        ("spool_usage_history", 687): "stop_terminal_unseen",
    }
    assert await _row(engine, "spool_usage_history", 687) is not None
    assert (await _row(engine, "spool", 319)).weight_used == pytest.approx(300.0)
    assert _ids(plan, frr.ReverseSpoolCharge) == {688}
    assert (await _row(engine, "spool", 320)).weight_used == pytest.approx(220.0)


async def test_failed_run_restores_failed_and_drops_the_replay_row_beside_its_own(engine: AsyncEngine) -> None:
    """(d)"""
    await _seed(engine, _shape_failed)
    plan = await _repair(engine)

    archive = await _row(engine, "print_archives", 131)
    assert (archive.status, archive.completed_at) == ("failed", T0)
    assert _ids(plan, frr.DropPrintLogEntry) == {532}
    assert _ids(plan, frr.RewritePrintLogEntry) == set()
    assert await _row(engine, "print_log_entries", 532) is None
    genuine = await _row(engine, "print_log_entries", 531)
    assert (genuine.status, genuine.failure_reason) == ("failed", "clog"), "the run's own row is untouched"
    assert _ids(plan, frr.ReverseSpoolCharge) == {632}
    assert await _row(engine, "spool_usage_history", 631) is not None, "the genuine failed charge stays"
    assert (await _row(engine, "spool", 304)).weight_used == pytest.approx(325.0)


async def test_retry_chain_leaves_the_parents_genuine_rows_alone(engine: AsyncEngine) -> None:
    """(e) The parent's stop-word charge at ITS terminal is 30 s from the parent's run, and hours
    from the child's. An archive-keyed rule would call it a replay of the archive's last run."""
    await _seed(engine, _shape_retry_chain)
    plan = await _repair(engine)

    assert _ids(plan, frr.ReverseSpoolCharge) == {643}
    assert await _row(engine, "spool_usage_history", 641) is not None
    assert await _row(engine, "spool_usage_history", 642) is not None
    assert (await _row(engine, "spool", 305)).weight_used == pytest.approx(540.0)
    assert (await _row(engine, "print_log_entries", 541)).status == "failed"
    child_row = await _row(engine, "print_log_entries", 542)
    assert (child_row.status, child_row.completed_at) == ("completed", T0 + 3 * H)
    reversal = next(a for a in plan.actions if isinstance(a, frr.ReverseSpoolCharge))
    assert reversal.unit_id == 241, "attributed to the child run, whose window holds it"
    archive = await _row(engine, "print_archives", 141)
    assert (archive.status, archive.completed_at) == ("completed", T0 + 3 * H), "its LAST run is the child"


async def test_locked_spent_and_archived_spools_are_skipped_and_logged(engine: AsyncEngine) -> None:
    """(f)"""
    await _seed(engine, _shape_retired_spools)
    plan = await _repair(engine)

    skips = {skip.usage_id: skip.code for skip in plan.skips if isinstance(skip, frr.SkipSpoolCharge)}
    assert skips == {651: "weight_locked", 652: "spent", 653: "archived"}
    assert _ids(plan, frr.ReverseSpoolCharge) == {654}
    for charge_id, spool_id in ((651, 306), (652, 307), (653, 308)):
        assert await _row(engine, "spool_usage_history", charge_id) is not None
        assert (await _row(engine, "spool", spool_id)).weight_used == pytest.approx(300.0)
    assert (await _row(engine, "spool", 309)).weight_used == pytest.approx(250.0)
    assert (await _row(engine, "print_archives", 151)).status == "completed", (
        "the archive repair does not wait on spools"
    )


async def test_duplicate_printing_archives_close_to_their_run_outcome_leaving_the_latest(engine: AsyncEngine) -> None:
    """(g) The index prerequisite: at most one ``printing`` archive per printer afterwards."""
    await _seed(engine, _shape_printing_duplicates)
    plan = await _repair(engine)

    assert _ids(plan, frr.ClosePrintingDuplicate) == {160, 161, 162, 164}
    expected = {
        160: ("completed", T0 - 2 * D + 4 * H),  # its run completed
        161: ("cancelled", NOW),  # its run is still marked printing: outcome unknown
        162: ("cancelled", NOW),  # no run at all
        164: ("cancelled", T0 - 5 * H),  # its run recorded cancelled
        163: ("printing", None),  # the latest start on printer 7
        170: ("printing", None),  # alone on printer 8
    }
    for archive_id, (status, completed_at) in expected.items():
        archive = await _row(engine, "print_archives", archive_id)
        assert (archive.status, archive.completed_at) == (status, completed_at), archive_id
    async with engine.connect() as conn:
        tab = Base.metadata.tables["print_archives"]
        printing = (await conn.execute(select(tab.c.printer_id).where(tab.c.status == "printing"))).scalars().all()
    assert sorted(printing) == [7, 8]


async def test_cross_printer_replay_keeps_the_runs_own_printer_row(engine: AsyncEngine) -> None:
    """(k) Two replay rows for one run: the one written on the run's printer is kept, even though
    it is the later one. (k2) A lone row from another printer is restated on the run's printer."""
    await _seed(engine, _shape_cross_printer)
    plan = await _repair(engine)

    assert _ids(plan, frr.RewritePrintLogEntry) == {582, 591}
    assert _ids(plan, frr.DropPrintLogEntry) == {581}
    assert _ids(plan, frr.ReverseSpoolCharge) == {681}, "a phantom charge is phantom on any printer"
    kept = await _row(engine, "print_log_entries", 582)
    assert (kept.status, kept.printer_id) == ("completed", 9)
    restated = await _row(engine, "print_log_entries", 591)
    assert (restated.status, restated.printer_id, restated.printer_name) == ("completed", 11, "011-H2S")


async def test_foreign_prints_are_never_touched_and_are_counted(engine: AsyncEngine) -> None:
    """(l) A row after an operator reprint belongs to that reprint, not to the farm's earlier run.
    Rows with no farm run are every foreign print ever logged, so the report counts them."""
    await _seed(engine, _shape_foreign)
    plan = await _repair(engine)

    assert plan.actions == ()
    assert plan.skips == ()
    tallies = dict(plan.tallies)
    assert (
        tallies["charge: written during its archive's last print, which was no queue unit (an operator reprint)"] == 1
    )
    assert tallies["charge: no farm run started before it (a foreign print)"] == 1
    assert tallies["print-log: no farm run started before it (a foreign print)"] == 1
    assert (await _row(engine, "spool", 311)).weight_used == pytest.approx(200.0)
    assert await _row(engine, "spool_usage_history", 695) is not None
    assert await _row(engine, "print_log_entries", 596) is not None


async def test_a_replay_inside_a_run_still_printing_is_listed_and_left_alone(engine: AsyncEngine) -> None:
    """(p) Replay-shaped, but not judgeable yet: the report lists it so a reader can follow it up."""
    await _seed(engine, _shape_run_still_printing)
    plan = await _repair(engine)

    assert plan.actions == ()
    assert _skip_codes(plan) == {
        ("print_log_entries", 594): "run_open",
        ("spool_usage_history", 692): "run_open",
    }
    assert (await _row(engine, "print_archives", 200)).status == "cancelled"
    assert (await _row(engine, "spool", 315)).weight_used == pytest.approx(250.0)


async def test_a_pre_0919_aborted_replay_is_repaired_like_any_other(engine: AsyncEngine) -> None:
    """(m) The status word is not the evidence: the write time against the run's terminal is."""
    await _seed(engine, _shape_pre_0919)
    plan = await _repair(engine)

    assert _ids(plan, frr.RestoreArchiveOutcome) == {197}
    assert _ids(plan, frr.RewritePrintLogEntry) == {597}
    assert _ids(plan, frr.ReverseSpoolCharge) == {699}
    archive = await _row(engine, "print_archives", 197)
    assert (archive.status, archive.completed_at) == ("completed", T0)
    entry = await _row(engine, "print_log_entries", 597)
    # No donor charge on printer 14: a completed run falls back to the archive's figure.
    assert (entry.status, entry.completed_at, entry.filament_used_grams) == ("completed", T0, 120.0)
    assert await _row(engine, "spool_usage_history", 699) is None
    assert (await _row(engine, "spool", 312)).weight_used == pytest.approx(375.0)


async def test_a_completed_replay_charge_is_reversed_and_the_genuine_one_at_the_terminal_kept(
    engine: AsyncEngine,
) -> None:
    """(n) Both charges say ``completed``. The one written 5 s after the run's terminal is the
    run's own; the one written a day later is the true-status branch charging the print twice."""
    await _seed(engine, _shape_true_status_beside_genuine)
    plan = await _repair(engine)

    assert _ids(plan, frr.ReverseSpoolCharge) == {690}
    assert await _row(engine, "spool_usage_history", 698) is not None, "the genuine charge at the terminal stays"
    assert await _row(engine, "spool_usage_history", 690) is None
    assert (await _row(engine, "spool", 313)).weight_used == pytest.approx(390.0)
    assert _ids(plan, frr.DropPrintLogEntry) == {598}, "a second completed row would count the plate twice"
    assert (await _row(engine, "print_log_entries", 599)).completed_at == T0, "the run's own row is untouched"
    assert _ids(plan, frr.RestoreArchiveOutcome) == set(), "a completed archive is not a replay's word"


async def test_a_completed_replay_row_that_is_the_runs_only_record_is_restated(engine: AsyncEngine) -> None:
    """(o) Same status before and after; the time, duration and grams become the run's own."""
    await _seed(engine, _shape_true_status_after_archiveless_terminal)
    plan = await _repair(engine)

    rewrite = next(a for a in plan.actions if isinstance(a, frr.RewritePrintLogEntry))
    assert rewrite.before.status == rewrite.after.status == "completed"
    entry = await _row(engine, "print_log_entries", 590)
    assert (entry.completed_at, entry.duration_seconds) == (T0, 4 * 3600)
    assert (entry.filament_used_grams, entry.cost) == (118.0, 2.9), "the real terminal's donor charge"
    assert _ids(plan, frr.ReverseSpoolCharge) == {691}
    assert (await _row(engine, "spool", 314)).weight_used == pytest.approx(205.0)
    assert await _row(engine, "spool_usage_history", 9002) is not None, "the real completion's donor charge stays"
    assert (await _plan(engine)).actions == (), "the restated row now reads as the run's record: no second rewrite"


async def test_a_second_plan_after_apply_has_no_actions(engine: AsyncEngine) -> None:
    """(h) Every shape at once; then the repair has nothing left to do, and skips stay skips."""
    await _seed(engine, *ALL_SHAPES)
    first = await _repair(engine)
    assert len(first.actions) > 0
    second = await _plan(engine)
    assert second.actions == ()
    assert second.skips == first.skips


async def test_apply_refuses_a_database_that_moved_since_the_plan(engine: AsyncEngine) -> None:
    """Apply is guarded on the plan's pre-image; the caller's transaction discards everything."""
    await _seed(engine, _shape_leaked)
    plan = await _plan(engine)
    spool = Base.metadata.tables["spool"]
    async with engine.begin() as conn:
        await conn.execute(update(spool).where(spool.c.id == 301).values(weight_used=700.0))

    with pytest.raises(frr.RepairDrift, match="UPDATE spool hit 0 rows"):
        async with engine.begin() as conn:
            await conn.run_sync(frr.apply_foreign_replay_repair, plan)

    assert (await _row(engine, "print_archives", 101)).status == "cancelled", "the archive restore rolled back with it"
    assert await _row(engine, "spool_usage_history", 601) is not None


async def test_two_reversals_on_one_spool_chain_their_guards(engine: AsyncEngine) -> None:
    """The second reversal's pre-image is the first one's after, and the floor is zero."""
    await _seed(engine, _shape_leaked)
    extra = _Seed()
    extra.charge(602, 301, 101, 1, grams=700.0, status="aborted", created_at=T0 + 3 * D)
    async with engine.begin() as conn:
        await extra.write(conn)
    plan = await _repair(engine)

    reversals = [a for a in plan.actions if isinstance(a, frr.ReverseSpoolCharge)]
    assert [(r.spool_weight_used_before, r.spool_weight_used_after) for r in reversals] == [
        (673.0, 618.0),
        (618.0, 0.0),
    ]
    assert (await _row(engine, "spool", 301)).weight_used == 0.0


@pytest.mark.parametrize(
    ("weight_before", "baseline_before", "weight_after", "baseline_after"),
    [
        (673.0, 650.0, 618.0, 595.0),  # reset after the phantom: the reset anchored it, both come down
        (673.0, 600.0, 618.0, 600.0),  # still at or above the baseline: the reset came before, kept
        (673.0, 0.0, 618.0, 0.0),  # never reset
        (40.0, 30.0, 0.0, 0.0),  # both floored at zero
    ],
    ids=["reset-after-phantom", "reset-before-phantom", "never-reset", "floored"],
)
async def test_a_reversal_below_the_usage_baseline_lowers_the_baseline_with_it(
    engine: AsyncEngine, weight_before: float, baseline_before: float, weight_after: float, baseline_after: float
) -> None:
    """Consumption since an operator's reset cannot be negative, so a reset that would read
    negative after the reversal anchored the phantom, and gives its grams back too."""
    await _seed(engine, _shape_leaked)
    spool = Base.metadata.tables["spool"]
    async with engine.begin() as conn:
        await conn.execute(
            update(spool)
            .where(spool.c.id == 301)
            .values(weight_used=weight_before, weight_used_baseline=baseline_before)
        )
    plan = await _repair(engine)

    reversal = next(a for a in plan.actions if isinstance(a, frr.ReverseSpoolCharge))
    assert (reversal.baseline_before, reversal.baseline_after) == (baseline_before, baseline_after)
    row = await _row(engine, "spool", 301)
    assert (row.weight_used, row.weight_used_baseline) == (pytest.approx(weight_after), pytest.approx(baseline_after))
    assert row.weight_used >= row.weight_used_baseline, "Total Consumed never reads negative"
    expected = (
        f"baseline {baseline_before:.1f} -> {baseline_after:.1f}"
        if baseline_after != baseline_before
        else f"baseline {baseline_before:.1f} kept"
    )
    assert expected in frr.describe(reversal)


async def test_apply_is_guarded_on_the_baseline_pre_image_too(engine: AsyncEngine) -> None:
    """An operator resetting Total Consumed between plan and apply moves the pre-image."""
    await _seed(engine, _shape_leaked)
    plan = await _plan(engine)
    spool = Base.metadata.tables["spool"]
    async with engine.begin() as conn:
        await conn.execute(update(spool).where(spool.c.id == 301).values(weight_used_baseline=673.0))

    with pytest.raises(frr.RepairDrift, match="UPDATE spool hit 0 rows"):
        async with engine.begin() as conn:
            await conn.run_sync(frr.apply_foreign_replay_repair, plan)

    assert (await _row(engine, "spool", 301)).weight_used == pytest.approx(673.0)


async def test_the_report_is_ascii_and_carries_every_action_and_skip(engine: AsyncEngine) -> None:
    await _seed(engine, *ALL_SHAPES)
    plan = await _plan(engine)
    report = frr.format_report(plan)

    assert report.isascii()
    for item in (*plan.actions, *plan.skips):
        assert frr.describe(item) in report
    assert "reverse spool charge (R-charge): 12" in report
    assert "grams reversed: 793.7 g across 12 spool(s)" in report
    assert "012-Drucker-\\xfc" in report, "a non-ASCII printer name is escaped, not dropped"


# --- the migration: ONE savepoint with the marker ---------------------------------------------------------


async def _marker_count(engine: AsyncEngine) -> int:
    settings = Base.metadata.tables["settings"]
    async with engine.connect() as conn:
        return len((await conn.execute(select(settings.c.id).where(settings.c.key == _MARKER))).all())


async def _migrate(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await run_migrations(conn)


@pytest.mark.asyncio
@pytest.mark.usefixtures("force_sqlite_dialect")
async def test_the_migration_rolls_back_whole_then_applies_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """(i) An injected failure after every statement has run leaves NOTHING behind and no marker;
    the next boot applies the repair and logs each action; the boot after that does nothing."""
    engine = await create_memory_engine()
    try:
        # A live install is migrated BEFORE the damage is written, so its archives went into the
        # ``archive_fts`` external-content index through the insert trigger, as production's did.
        # Seeding before the first migration would leave the index EMPTY, and FTS5 then reports
        # "database disk image is malformed" at the update trigger's 'delete'. That only happens
        # with nothing indexed at all (measured on SQLite 3.50.4), so it is a fixture artifact, as
        # in ``test_id_reuse_autoincrement_migration``. This first boot has nothing to repair and
        # writes the marker. Removing the marker stands for "the build carrying the repair has not
        # booted yet".
        await _migrate(engine)
        assert await _marker_count(engine) == 1
        settings = Base.metadata.tables["settings"]
        seed = _Seed()
        _shape_leaked(seed)
        async with engine.begin() as conn:
            await conn.execute(delete(settings).where(settings.c.key == _MARKER))
            await seed.write(conn)
        expected = await _plan(engine)
        assert len(expected.actions) == 3

        real_apply = frr.apply_foreign_replay_repair

        def _apply_then_fail(conn: Connection, plan: frr.RepairPlan) -> None:
            real_apply(conn, plan)
            raise RuntimeError("injected failure after the last statement")

        monkeypatch.setattr(frr, "apply_foreign_replay_repair", _apply_then_fail)
        with caplog.at_level(logging.WARNING):
            await _migrate(engine)  # must NOT raise
        assert f"{_MARKER} failed and was rolled back" in caplog.text
        assert not any(frr.describe(action) in caplog.text for action in expected.actions), (
            "a rolled-back boot must not read as a repaired one"
        )
        assert await _marker_count(engine) == 0
        assert (await _row(engine, "print_archives", 101)).status == "cancelled"
        assert (await _row(engine, "print_log_entries", 501)).status == "cancelled"
        assert await _row(engine, "spool_usage_history", 601) is not None
        assert (await _row(engine, "spool", 301)).weight_used == pytest.approx(673.0)

        monkeypatch.setattr(frr, "apply_foreign_replay_repair", real_apply)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            await _migrate(engine)
        assert await _marker_count(engine) == 1
        assert (await _row(engine, "print_archives", 101)).status == "completed"
        assert await _row(engine, "spool_usage_history", 601) is None
        assert (await _row(engine, "spool", 301)).weight_used == pytest.approx(618.0)
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        for action in expected.actions:
            assert f"[REPAIR] {_MARKER}: {frr.describe(action)}" in warnings, "the per-row log is the report's body"

        caplog.clear()
        with caplog.at_level(logging.INFO):
            await _migrate(engine)
        assert f"[REPAIR] {_MARKER}" not in caplog.text, "the marker makes a later boot a no-op"
    finally:
        await engine.dispose()


# --- the report runner: the live file is never written ------------------------------------------------------


async def _live_db(tmp_path: Path, *ddl: str) -> Path:
    """A 'live' database FILE holding shape (a), plus any extra DDL, closed before the run.

    The runner reads a file through the backup API, so the fixture's in-memory schema is seeded and
    then written out whole with ``VACUUM INTO``: a standalone file, no journal left beside it, and
    no second schema build."""
    live = tmp_path / "live"
    live.mkdir()
    db_path = live / "bambuddy.db"
    seed = _Seed()
    _shape_leaked(seed)
    engine = await create_memory_engine()
    try:
        async with engine.begin() as conn:
            await seed.write(conn)
            for statement in ddl:
                await conn.exec_driver_sql(statement)
        async with engine.connect() as conn:
            await conn.exec_driver_sql("VACUUM INTO ?", (str(db_path),))
    finally:
        await engine.dispose()
    return db_path


def _run_report(tmp_path: Path, db_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    """Run exactly as on the farm PC: a separate process, isolated mode, the module by path."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update(TMP=str(scratch), TEMP=str(scratch), TMPDIR=str(scratch))
    return subprocess.run(
        [sys.executable, "-I", str(_RUNNER_PATH), str(db_path), "--now", "2026-09-25T12:00:00", *extra],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _assert_source_untouched_and_copy_gone(tmp_path: Path, db_path: Path, before: tuple[bytes, int]) -> None:
    assert (db_path.read_bytes(), db_path.stat().st_mtime_ns) == before, "the live database is unchanged"
    assert sorted(p.name for p in db_path.parent.iterdir()) == ["bambuddy.db"], "no journal or WAL left beside it"
    assert list((tmp_path / "scratch").iterdir()) == [], "the temporary copy is deleted"


async def test_the_report_runner_reads_a_copy_and_leaves_the_source_untouched(tmp_path: Path) -> None:
    """(j) The plain report, then the rehearsal: neither writes the source, both delete the copy."""
    db_path = await _live_db(tmp_path)
    before = (db_path.read_bytes(), db_path.stat().st_mtime_ns)

    report = _run_report(tmp_path, db_path)
    assert report.returncode == 0, report.stderr
    assert "reverse-charge usage 601 spool 301 archive 101: 55.0 g cancelled" in report.stdout
    assert "actions: 3" in report.stdout
    assert "REHEARSAL" not in report.stdout
    _assert_source_untouched_and_copy_gone(tmp_path, db_path, before)

    rehearsal = _run_report(tmp_path, db_path, "--apply-on-copy")
    assert rehearsal.returncode == 0, rehearsal.stdout + rehearsal.stderr
    assert "OK: applied 3 action(s); a second plan finds 0" in rehearsal.stdout
    _assert_source_untouched_and_copy_gone(tmp_path, db_path, before)


async def test_a_failing_rehearsal_exits_non_zero_and_still_leaves_the_source_untouched(tmp_path: Path) -> None:
    """A write the real rows refuse (here, a trigger) is what the rehearsal exists to surface."""
    db_path = await _live_db(
        tmp_path,
        "CREATE TRIGGER refuse_spool_update BEFORE UPDATE ON spool BEGIN SELECT RAISE(ABORT, 'refused'); END",
    )
    before = (db_path.read_bytes(), db_path.stat().st_mtime_ns)

    rehearsal = _run_report(tmp_path, db_path, "--apply-on-copy")

    assert rehearsal.returncode == 1
    assert "FAILED: IntegrityError" in rehearsal.stdout and "refused" in rehearsal.stdout
    _assert_source_untouched_and_copy_gone(tmp_path, db_path, before)
