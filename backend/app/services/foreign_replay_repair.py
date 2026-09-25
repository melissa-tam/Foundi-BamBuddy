"""One-time repair of the damage the downtime reconcile's replays did (2026-09-16 .. 2026-09-24).

At every farm restart or MQTT reconnect, ``main.reconcile_stale_active_prints`` replayed each
archive still in ``printing`` as a synthesised terminal. The archive was usually only LEAKED in
that state: the real completion had already run, but ``on_print_complete`` could not find the
archive after a restart. So the real completion charged filament through the dispatch donor
with ``archive_id=None`` and wrote NO print-log row. The replay then:

* charged filament to spools from the LIVE printer's progress, which belonged to a different,
  running print (a phantom charge);
* closed the archive and wrote a print-log row, both ``cancelled`` (``aborted`` before
  2026-09-19). When the printer still showed that job's FINISH/FAILED it wrote the true status
  instead, and charged the print a second time;
* in 10 of 50 production cases closed the WRONG archive: the newest same-named ``printing``
  archive, which was the CURRENT print's (the misbinding), sometimes on another printer.

This module plans and applies the ONE guarded repair of both damage sets (the user's ruling).

**Standalone by construction.** It imports only the standard library and SQLAlchemy Core, never
``backend.app``, and works on a SYNC ``Connection`` handed in, with no service side effects.
The same file therefore runs by path under the farm PC's embedded Python in report mode
(``backend/scripts/foreign_replay_report.py``, against a backup-API COPY of the live DB), and
inside ``core.database.run_migrations`` through ``AsyncConnection.run_sync``. The table stubs
below name only the columns the repair reads or writes. ``test_foreign_replay_repair`` pins
every stub column against the live models, so a rename fails CI instead of the repair.

**Runs, not archives.** Archives are reused across retries and reprints, and
``queue_builder.requeue_fields`` carries ``archive_id`` onto a retry. So an archive's
``subtask_id`` names only its LAST print, and keying any rule on the archive alone would charge
one run's evidence to another. A RUN of archive A is a queue unit U on A's printer with
``U.dispatch_subtask_id == A.subtask_id`` or ``U.archive_id == A.id``, windowed
``[U.started_at, U.completed_at]``. When A's ``subtask_id`` matches no unit on its printer, A's
last print was not a farm unit (an operator reprint), so that print is a FOREIGN run starting at
``A.started_at``. Rows it owns are foreign and never touched. A row is attributed to the run
whose window holds it, or else to the latest run that ended before it with no later run started
before it. Both cases reduce to one rule: the run with the latest start at or before the row.

**Why 180 s identifies a replay, whatever the row or the run recorded.** Charges (the three
``SpoolUsageHistory`` writers in ``usage_tracker``) and print-log rows (``main.on_print_complete``'s
one ``write_log_entry``) are written ONLY inside a terminal chain. That chain stamps a still
``printing`` unit's ``completed_at`` seconds before writing them. The only other writers of a
terminal unit's ``completed_at`` are pre-start dispatch failures and pending-unit cancellations,
which never print and so write no rows. A row attributed to a terminal run but written further
than ``TERMINAL_WINDOW`` from that terminal therefore came from ANOTHER terminal, a replay. That
holds for a run recorded ``cancelled`` too. A run's outcome was unobserved only when the replay
itself was its terminal: the replay then matched the still-printing unit and stamped its
``completed_at`` in the same chain, so its rows sit inside the window and are the run's own.
When the unit had already ended, the replay resolved FOREIGN and left it alone. The unit kept
its earlier real terminal, and the replay's rows fall outside the window. The rules judge by
that evidence, never by a status word.

**The one writer that breaks that premise.** The queue page's Stop
(``routes/print_queue.stop_queue_item``) commits ``cancelled``, ``completed_at`` and
``stop_source='operator_ui'`` BEFORE the printer's terminal arrives. The terminal chain then
leaves that ``completed_at`` alone. The terminal normally follows within seconds. But a Stop sent
to an offline printer never reaches it, and the print's real terminal can come hours later.
So, for such a unit, the window is evidence only once the ledger shows its terminal there: a
charge or print-log row on its printer within the window of that ``completed_at``. Without
that, a row outside the window cannot be told from a late real terminal. It is listed
(``stop_terminal_unseen``), never reversed.

**The rules** (see each ``_plan_*`` method):

* R-dup: per printer, every ``printing`` archive except the one with the latest start is closed.
  This is the prerequisite for a unique index of one printing archive per printer. The single
  latest one is left for the runtime to heal.
* R-archive: a ``cancelled`` or ``aborted`` archive whose last run recorded ``completed``/``failed``
  takes that outcome.
* R-log: a print-log row that is a replay of a terminal run is rewritten to the run's outcome,
  or dropped when the run already has its own row. One run keeps at most one row. Its grams are
  the run's real terminal's own charges (see ``_Planner._rewrite``).
* R-charge: a charge that is a replay of a terminal run is reversed and deleted. It is skipped
  when the spool is weight-locked, spent or archived. A reversal that takes ``weight_used``
  below the spool's ``weight_used_baseline`` also lowers the baseline by the same grams,
  floored at 0. Consumption since an operator's "Reset Total Consumed" cannot be negative, so
  such a reset anchored a value that already held the phantom.

Rows written at their run's own terminal are the run's record, whatever it recorded, and are
never touched. So are rows with no farm run to judge them by (foreign prints, rows whose
archive is gone); these are counted in the report.

**Plan, then apply.** ``plan_foreign_replay_repair`` only SELECTs, re-verifying each fact per
row, and returns a closed set of typed actions carrying their before-values.
``apply_foreign_replay_repair`` executes exactly that list. Every statement is guarded on its
pre-image and must hit exactly one row, so a DB that moved between plan and apply raises
``RepairDrift`` and the caller's savepoint discards the whole repair. After an apply, a new
plan has no actions.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, Literal, TypeAlias

from sqlalchemy import (
    Boolean,
    Column,
    Connection,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    delete,
    func,
    select,
    update,
)

#: A row written further than this from its run's recorded terminal came from another terminal.
TERMINAL_WINDOW: Final = timedelta(seconds=180)

#: Spool weights are compared on apply with this slack, because they are float columns.
_WEIGHT_EPSILON: Final = 1e-6

# --- the exact strings this repair matches -------------------------------------------------
#
# These are local by necessity and are not an outcome fold. The module cannot import
# ``services.print_log`` (it must run by path with no ``backend.app`` on the path), and it
# never asks "how did this print turn out". It matches the outcome the queue unit RECORDED
# (``print_queue.status``, a different column with its own vocabulary) and the word a replay
# wrote on an archive it closed.
_ARCHIVE_PRINTING: Final = "printing"
_COMPLETED: Final = "completed"
_FAILED: Final = "failed"
_CANCELLED: Final = "cancelled"
#: The client's raw word for a job that ended in neither FINISH nor FAILED. Until 2026-09-19 an
#: unattributed replay kept it on the archive it closed. ``cancelled`` replaced it after that.
_ABORTED: Final = "aborted"
#: What an unattributed replay wrote on an archive it closed. R-archive acts only on these. An
#: archive holds one status, overwritten by whichever terminal wrote last, so there is no second
#: row to compare against: a status a replay never writes is not the repair's to change.
_REPLAY_ARCHIVE_STATUSES: Final = frozenset({_CANCELLED, _ABORTED})
#: A unit outcome R-archive restores onto an archive a replay closed.
_RESTORABLE_OUTCOMES: Final = frozenset({_COMPLETED, _FAILED})
#: Unit outcomes that end a run. R-dup closes a superseded archive to one of these, and a row
#: outside such a run's terminal window is a replay.
_TERMINAL_UNIT_STATUSES: Final = frozenset({_COMPLETED, _FAILED, _CANCELLED})
#: ``print_queue.stop_source`` of the queue page's Stop, whose ``completed_at`` precedes the terminal.
_STOP_OPERATOR_UI: Final = "operator_ui"
#: Printer subtask ids that name no dispatch: a screen-started print echoes "0".
_NO_SUBTASK: Final = frozenset({"", "0"})

# --- table stubs: only the columns this repair reads or writes -------------------------------
_metadata = MetaData()
_archives = Table(
    "print_archives",
    _metadata,
    Column("id", Integer, primary_key=True),
    Column("printer_id", Integer),
    Column("status", String),
    Column("subtask_id", String),
    Column("started_at", DateTime),
    Column("completed_at", DateTime),
    Column("filament_used_grams", Float),
    Column("cost", Float),
    Column("failure_reason", String),
)
_units = Table(
    "print_queue",
    _metadata,
    Column("id", Integer, primary_key=True),
    Column("printer_id", Integer),
    Column("archive_id", Integer),
    Column("dispatch_subtask_id", String),
    Column("status", String),
    Column("stop_source", String),
    Column("started_at", DateTime),
    Column("completed_at", DateTime),
)
_log = Table(
    "print_log_entries",
    _metadata,
    Column("id", Integer, primary_key=True),
    Column("archive_id", Integer),
    Column("printer_id", Integer),
    Column("printer_name", String),
    Column("status", String),
    Column("started_at", DateTime),
    Column("completed_at", DateTime),
    Column("duration_seconds", Integer),
    Column("filament_used_grams", Float),
    Column("cost", Float),
    Column("failure_reason", String),
    Column("created_at", DateTime),
)
_charges = Table(
    "spool_usage_history",
    _metadata,
    Column("id", Integer, primary_key=True),
    Column("spool_id", Integer),
    Column("printer_id", Integer),
    Column("archive_id", Integer),
    Column("weight_used", Float),
    Column("cost", Float),
    Column("status", String),
    Column("created_at", DateTime),
)
_spools = Table(
    "spool",
    _metadata,
    Column("id", Integer, primary_key=True),
    Column("weight_used", Float),
    Column("weight_used_baseline", Float),
    Column("weight_locked", Boolean),
    Column("spent_at", DateTime),
    Column("archived_at", DateTime),
)
_printers = Table(
    "printers",
    _metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String),
)

#: The stubbed schema, by table: what ``test_foreign_replay_repair`` pins against the models.
STUB_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    table.name: tuple(column.name for column in table.columns) for table in _metadata.sorted_tables
}


# --- actions: the closed set apply executes ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClosePrintingDuplicate:
    """R-dup: a ``printing`` archive superseded on its printer by a later one."""

    archive_id: int
    printer_id: int
    started_at: datetime | None
    kept_archive_id: int
    unit_id: int | None
    to_status: str
    completed_at: datetime
    failure_reason_before: str | None


@dataclass(frozen=True, slots=True)
class RestoreArchiveOutcome:
    """R-archive: a replay-cancelled archive takes its last run's recorded outcome."""

    archive_id: int
    unit_id: int
    from_status: str
    to_status: str
    completed_at_before: datetime | None
    completed_at: datetime
    failure_reason_before: str | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class LogEntryFields:
    """The print-log fields R-log may rewrite, as one before or after image."""

    status: str
    completed_at: datetime | None
    duration_seconds: int | None
    filament_used_grams: float | None
    cost: float | None
    failure_reason: str | None
    printer_id: int | None
    printer_name: str | None


@dataclass(frozen=True, slots=True)
class RewritePrintLogEntry:
    """R-log: the one replay row of a run with no row of its own, rewritten to the run's outcome."""

    entry_id: int
    archive_id: int
    unit_id: int
    created_at: datetime
    before: LogEntryFields
    after: LogEntryFields


@dataclass(frozen=True, slots=True)
class DropPrintLogEntry:
    """R-log: a replay row of a run that is already recorded by another row."""

    entry_id: int
    archive_id: int
    unit_id: int
    status: str
    created_at: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class ReverseSpoolCharge:
    """R-charge: a phantom charge, whose grams go back to the spool and whose row is deleted.

    The inventory's "Total Consumed" reads ``weight_used - weight_used_baseline``, and an
    operator's reset stamps the baseline to the ``weight_used`` of that moment. When a
    reversal would leave ``weight_used`` below the baseline, the reset happened AFTER the
    phantom: consumption since a reset cannot be negative. The reset therefore anchored the
    phantom's grams too, and the baseline comes down by the same grams, floored at 0. Otherwise
    it stays where the operator put it. ``run_status``/``run_completed_at`` name the run
    terminal the charge was judged against, so a reader can check the gap.
    """

    usage_id: int
    spool_id: int
    archive_id: int
    unit_id: int
    run_status: str
    run_completed_at: datetime
    charge_status: str
    created_at: datetime
    grams: float
    spool_weight_used_before: float
    spool_weight_used_after: float
    baseline_before: float
    baseline_after: float


RepairAction: TypeAlias = (
    ClosePrintingDuplicate | RestoreArchiveOutcome | RewritePrintLogEntry | DropPrintLogEntry | ReverseSpoolCharge
)


# --- skips: reported, never applied ------------------------------------------------------------

SpoolSkipCode = Literal["spool_missing", "weight_locked", "spent", "archived"]
#: Replay-shaped rows the rules cannot judge: their run has not recorded its terminal yet, or its
#: recorded ``completed_at`` is a queue-page Stop's with no terminal seen beside it.
RowSkipCode = Literal["run_open", "run_time_unknown", "stop_terminal_unseen"]
SkippedTable = Literal["print_archives", "print_log_entries", "spool_usage_history"]


@dataclass(frozen=True, slots=True)
class SkipSpoolCharge:
    """A replay charge left in place because the spool's weight is no longer the repair's to move."""

    usage_id: int
    spool_id: int
    archive_id: int
    grams: float
    code: SpoolSkipCode
    detail: str


@dataclass(frozen=True, slots=True)
class SkipRow:
    """A candidate row the rules decline, with the reason. Nothing is written for it."""

    table: SkippedTable
    row_id: int
    archive_id: int | None
    code: RowSkipCode
    detail: str


RepairSkip: TypeAlias = SkipSpoolCharge | SkipRow


@dataclass(frozen=True, slots=True)
class RepairPlan:
    """What one plan found: the actions to apply, the skips to report, and what was examined.

    ``tallies`` counts the rows and archives left alone for a reason that needs no reader. They
    were judged GENUINE (a row written at its own run's terminal, whatever the run recorded),
    or they have no farm run to judge them by (a foreign print, a row whose archive is gone).
    They are counted rather than listed because they are every print the farm ever logged, and
    listing them would bury the damage.
    """

    now: datetime
    actions: tuple[RepairAction, ...]
    skips: tuple[RepairSkip, ...]
    tallies: tuple[tuple[str, int], ...]


class RepairDrift(RuntimeError):
    """An apply statement found the database no longer matching the plan's pre-image."""


# --- facts: the typed snapshot the planner reads -----------------------------------------------


@dataclass(frozen=True, slots=True)
class _Archive:
    id: int
    printer_id: int | None
    status: str
    subtask_id: str | None
    started_at: datetime | None
    completed_at: datetime | None
    filament_used_grams: float | None
    cost: float | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class _Unit:
    id: int
    printer_id: int
    archive_id: int | None
    dispatch_subtask_id: str | None
    status: str
    stop_source: str | None
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class _Run:
    """A window of one print on an archive's printer. ``unit`` is None for a FOREIGN run."""

    unit: _Unit | None
    started_at: datetime


@dataclass(frozen=True, slots=True)
class _LogRow:
    id: int
    archive_id: int | None
    printer_id: int | None
    printer_name: str | None
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    duration_seconds: int | None
    filament_used_grams: float | None
    cost: float | None
    failure_reason: str | None
    created_at: datetime | None

    def fields(self) -> LogEntryFields:
        return LogEntryFields(
            status=self.status,
            completed_at=self.completed_at,
            duration_seconds=self.duration_seconds,
            filament_used_grams=self.filament_used_grams,
            cost=self.cost,
            failure_reason=self.failure_reason,
            printer_id=self.printer_id,
            printer_name=self.printer_name,
        )


@dataclass(frozen=True, slots=True)
class _Charge:
    id: int
    spool_id: int
    archive_id: int
    weight_used: float
    status: str
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class _DonorCharge:
    """A charge the terminal chain wrote with no archive: the real completion of a leaked print,
    which charged through the dispatch donor because it could not find its archive."""

    weight_used: float
    cost: float | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _Spool:
    id: int
    weight_used: float
    weight_used_baseline: float
    weight_locked: bool
    spent_at: datetime | None
    archived_at: datetime | None


def _naive_utc(value: datetime | None) -> datetime | None:
    """Every datetime this repair compares is naive UTC, which is what the database stores."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _subtask(value: str | None) -> str | None:
    """A subtask id that names a dispatch, or None (unset, blank, or a screen print's "0")."""
    normalized = (value or "").strip()
    return None if normalized in _NO_SUBTASK else normalized


def _float(value: float | None) -> float:
    return float(value) if value is not None else 0.0


class _Facts:
    """One consistent read of every table the rules consult, indexed for run attribution."""

    def __init__(self, conn: Connection) -> None:
        self.archives: dict[int, _Archive] = {
            row.id: _Archive(
                id=row.id,
                printer_id=row.printer_id,
                status=row.status,
                subtask_id=row.subtask_id,
                started_at=_naive_utc(row.started_at),
                completed_at=_naive_utc(row.completed_at),
                filament_used_grams=row.filament_used_grams,
                cost=row.cost,
                failure_reason=row.failure_reason,
            )
            for row in conn.execute(select(_archives))
        }
        units = [
            _Unit(
                id=row.id,
                printer_id=row.printer_id,
                archive_id=row.archive_id,
                dispatch_subtask_id=_subtask(row.dispatch_subtask_id),
                status=row.status,
                stop_source=row.stop_source,
                started_at=_naive_utc(row.started_at),
                completed_at=_naive_utc(row.completed_at),
            )
            for row in conn.execute(select(_units).where(_units.c.printer_id.is_not(None)))
        ]
        self._by_subtask: dict[tuple[int, str], list[_Unit]] = defaultdict(list)
        self._by_archive: dict[tuple[int, int], list[_Unit]] = defaultdict(list)
        for unit in units:
            if unit.dispatch_subtask_id is not None:
                self._by_subtask[(unit.printer_id, unit.dispatch_subtask_id)].append(unit)
            if unit.archive_id is not None:
                self._by_archive[(unit.printer_id, unit.archive_id)].append(unit)
        self.log_rows: list[_LogRow] = [
            _LogRow(
                id=row.id,
                archive_id=row.archive_id,
                printer_id=row.printer_id,
                printer_name=row.printer_name,
                status=row.status,
                started_at=_naive_utc(row.started_at),
                completed_at=_naive_utc(row.completed_at),
                duration_seconds=row.duration_seconds,
                filament_used_grams=row.filament_used_grams,
                cost=row.cost,
                failure_reason=row.failure_reason,
                created_at=_naive_utc(row.created_at),
            )
            for row in conn.execute(select(_log).order_by(_log.c.id))
        ]
        self.charges: list[_Charge] = []
        self._donors: dict[int, list[_DonorCharge]] = defaultdict(list)
        # When a terminal chain wrote anything on a printer: every charge and print-log row.
        self._marks: dict[int, list[datetime]] = defaultdict(list)
        for row in conn.execute(select(_charges).order_by(_charges.c.id)):
            created_at = _naive_utc(row.created_at)
            if row.printer_id is not None and created_at is not None:
                self._marks[row.printer_id].append(created_at)
            if row.archive_id is not None:
                self.charges.append(
                    _Charge(
                        id=row.id,
                        spool_id=row.spool_id,
                        archive_id=row.archive_id,
                        weight_used=_float(row.weight_used),
                        status=row.status,
                        created_at=created_at,
                    )
                )
            elif row.printer_id is not None and created_at is not None:
                self._donors[row.printer_id].append(
                    _DonorCharge(weight_used=_float(row.weight_used), cost=row.cost, created_at=created_at)
                )
        for entry in self.log_rows:
            if entry.printer_id is not None and entry.created_at is not None:
                self._marks[entry.printer_id].append(entry.created_at)
        self.spools: dict[int, _Spool] = {
            row.id: _Spool(
                id=row.id,
                weight_used=_float(row.weight_used),
                weight_used_baseline=_float(row.weight_used_baseline),
                weight_locked=bool(row.weight_locked),
                spent_at=row.spent_at,
                archived_at=row.archived_at,
            )
            for row in conn.execute(select(_spools))
        }
        self.printer_names: dict[int, str | None] = {row.id: row.name for row in conn.execute(select(_printers))}

    def donor_charges(self, unit: _Unit) -> list[_DonorCharge]:
        """The run's real terminal's own archive-less charges: on its printer, within its window."""
        if unit.completed_at is None:
            return []
        return [
            donor
            for donor in self._donors.get(unit.printer_id, [])
            if abs(donor.created_at - unit.completed_at) <= TERMINAL_WINDOW
        ]

    def terminal_seen(self, unit: _Unit) -> bool:
        """Did any terminal chain write on the unit's printer within the window of its ``completed_at``?"""
        if unit.completed_at is None:
            return False
        return any(abs(mark - unit.completed_at) <= TERMINAL_WINDOW for mark in self._marks.get(unit.printer_id, []))

    def last_run(self, archive: _Archive) -> _Unit | None:
        """The unit that printed A's LAST print: the one its ``subtask_id`` names, on its printer."""
        subtask = _subtask(archive.subtask_id)
        if archive.printer_id is None or subtask is None:
            return None
        units = self._by_subtask.get((archive.printer_id, subtask), [])
        return max(units, key=lambda u: (u.started_at or datetime.min, u.id), default=None)

    def runs_of(self, archive: _Archive) -> list[_Run]:
        """Every run of A with a start, oldest first, and the foreign run of an operator reprint."""
        if archive.printer_id is None:
            return []
        units: dict[int, _Unit] = {u.id: u for u in self._by_archive.get((archive.printer_id, archive.id), [])}
        subtask = _subtask(archive.subtask_id)
        matched = self._by_subtask.get((archive.printer_id, subtask), []) if subtask is not None else []
        units.update((u.id, u) for u in matched)
        runs = [_Run(unit=u, started_at=u.started_at) for u in units.values() if u.started_at is not None]
        # A's subtask names its LAST print. When that names no unit, the last print was not the
        # farm's, and rows from its start on belong to it, never to an earlier farm run. It sorts
        # after a unit that started at the same instant, because it is the later print by definition.
        if (archive.subtask_id or "").strip() and not matched and archive.started_at is not None:
            runs.append(_Run(unit=None, started_at=archive.started_at))
        runs.sort(key=lambda r: (r.started_at, r.unit is None, r.unit.id if r.unit is not None else 0))
        return runs


def _attribute(runs: Sequence[_Run], at: datetime) -> _Run | None:
    """The run a row written at ``at`` belongs to: the latest one started at or before it.

    That is the run whose window holds ``at`` when one does, and otherwise the latest run that
    ended before ``at`` with no later run started before it. The two cases in the rule collapse
    to this, because on one printer the latest-started run is the only one that can still be
    open.
    """
    owner: _Run | None = None
    for run in runs:
        if run.started_at <= at:
            owner = run
    return owner


# --- the planner -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Verdict:
    """Where one candidate row landed: a replay of ``unit``, a skip, or genuine (``tally``)."""

    unit: _Unit | None = None
    skip: tuple[RowSkipCode, str] | None = None
    tally: str | None = None


_TALLY_OWN_TERMINAL: Final = "genuine: written at its run's own terminal"
_TALLY_NO_RUN: Final = "no farm run started before it (a foreign print)"
_TALLY_FOREIGN_RUN: Final = "written during its archive's last print, which was no queue unit (an operator reprint)"
_TALLY_ARCHIVE_GONE: Final = "its archive row is gone (nothing to judge it by)"


class _Planner:
    def __init__(self, facts: _Facts, now: datetime) -> None:
        self._facts = facts
        self._now = now
        self._actions: list[RepairAction] = []
        self._skips: list[RepairSkip] = []
        self._tallies: dict[str, int] = defaultdict(int)

    def plan(self) -> RepairPlan:
        self._plan_printing_duplicates()
        self._plan_archive_outcomes()
        self._plan_print_log()
        self._plan_charges()
        return RepairPlan(
            now=self._now,
            actions=tuple(self._actions),
            skips=tuple(self._skips),
            tallies=tuple(sorted(self._tallies.items())),
        )

    def _count(self, label: str) -> None:
        self._tallies[label] += 1

    def _judge(self, archive: _Archive, at: datetime | None) -> _Verdict:
        """Attribute a row against ``archive`` written at ``at`` to its run, and judge it.

        Returns the run's unit when the row is a replay of a terminal run, which is the only case
        a rule acts on. Neither the row's status nor the run's plays a part: a row inside the
        run's own terminal window is the run's record (a genuinely unobserved outcome included,
        because then the replay WAS the terminal), and a row outside it came from another
        terminal. See the module docstring.
        """
        if at is None:
            return _Verdict(skip=("run_time_unknown", "the row carries no write time"))
        run = _attribute(self._facts.runs_of(archive), at)
        if run is None:
            return _Verdict(tally=_TALLY_NO_RUN)
        unit = run.unit
        if unit is None:
            return _Verdict(tally=_TALLY_FOREIGN_RUN)
        if unit.status not in _TERMINAL_UNIT_STATUSES:
            return _Verdict(skip=("run_open", f"its run, unit {unit.id}, is still {unit.status}"))
        if unit.completed_at is None:
            return _Verdict(skip=("run_time_unknown", f"unit {unit.id} is {unit.status} with no completed_at"))
        if abs(at - unit.completed_at) <= TERMINAL_WINDOW:
            return _Verdict(tally=_TALLY_OWN_TERMINAL)
        if unit.stop_source == _STOP_OPERATOR_UI and not self._facts.terminal_seen(unit):
            return _Verdict(
                skip=(
                    "stop_terminal_unseen",
                    f"unit {unit.id} was stopped from the queue page at {_fmt(unit.completed_at)} and no terminal "
                    "was written beside it; a late real terminal cannot be told from a replay",
                )
            )
        return _Verdict(unit=unit)

    # R-dup ------------------------------------------------------------------------------------
    def _plan_printing_duplicates(self) -> None:
        by_printer: dict[int, list[_Archive]] = defaultdict(list)
        for archive in self._facts.archives.values():
            if archive.status == _ARCHIVE_PRINTING and archive.printer_id is not None:
                by_printer[archive.printer_id].append(archive)
        for printer_id in sorted(by_printer):
            printing = sorted(
                by_printer[printer_id],
                key=lambda a: (a.started_at is not None, a.started_at or datetime.min, a.id),
            )
            kept = printing[-1]
            for archive in printing[:-1]:
                unit = self._facts.last_run(archive)
                if unit is not None and unit.status in _TERMINAL_UNIT_STATUSES:
                    to_status, completed_at, unit_id = unit.status, unit.completed_at or self._now, unit.id
                else:
                    # Superseded by construction, and no recorded outcome to restore: unknown.
                    to_status, completed_at, unit_id = _CANCELLED, self._now, unit.id if unit else None
                self._actions.append(
                    ClosePrintingDuplicate(
                        archive_id=archive.id,
                        printer_id=printer_id,
                        started_at=archive.started_at,
                        kept_archive_id=kept.id,
                        unit_id=unit_id,
                        to_status=to_status,
                        completed_at=completed_at,
                        failure_reason_before=archive.failure_reason,
                    )
                )

    # R-archive --------------------------------------------------------------------------------
    def _plan_archive_outcomes(self) -> None:
        for archive in sorted(self._facts.archives.values(), key=lambda a: a.id):
            if archive.status not in _REPLAY_ARCHIVE_STATUSES:
                continue
            unit = self._facts.last_run(archive)
            if unit is None:
                self._count("cancelled/aborted archive with no run unit (foreign or never dispatched)")
                continue
            if unit.status not in _RESTORABLE_OUTCOMES:
                self._count(f"cancelled/aborted archive whose last run recorded {unit.status}")
                continue
            if unit.completed_at is None:
                self._skip(
                    "print_archives", archive.id, archive.id, "run_time_unknown", f"unit {unit.id} has no completed_at"
                )
                continue
            self._actions.append(
                RestoreArchiveOutcome(
                    archive_id=archive.id,
                    unit_id=unit.id,
                    from_status=archive.status,
                    to_status=unit.status,
                    completed_at_before=archive.completed_at,
                    completed_at=unit.completed_at,
                    failure_reason_before=archive.failure_reason,
                    failure_reason=None if unit.status == _COMPLETED else archive.failure_reason,
                )
            )

    # R-log --------------------------------------------------------------------------------------
    def _plan_print_log(self) -> None:
        replays: dict[int, list[tuple[_LogRow, _Archive]]] = defaultdict(list)
        units: dict[int, _Unit] = {}
        for entry in self._facts.log_rows:
            if entry.archive_id is None:
                continue
            archive = self._facts.archives.get(entry.archive_id)
            if archive is None:
                self._count(f"print-log: {_TALLY_ARCHIVE_GONE}")
                continue
            verdict = self._judge(archive, entry.created_at)
            if verdict.tally is not None:
                self._count(f"print-log: {verdict.tally}")
                continue
            if verdict.skip is not None:
                self._skip("print_log_entries", entry.id, archive.id, *verdict.skip)
                continue
            if verdict.unit is not None:
                replays[verdict.unit.id].append((entry, archive))
                units[verdict.unit.id] = verdict.unit

        for unit_id in sorted(replays):
            self._plan_run_log(units[unit_id], replays[unit_id])

    def _plan_run_log(self, unit: _Unit, members: list[tuple[_LogRow, _Archive]]) -> None:
        """One run's replay rows: at most one survives, rewritten, and only if the run has no row.

        A run's own row is one with the run's status, written within its terminal window, on its
        printer or against one of these archives. It is not keyed on the archive alone, because
        the real completion may have logged against another archive of the same run.

        A replay row keeps its write time when rewritten, so it stays a member of its run's group
        forever. Once it already reads as the run's record, the rewrite is a no-op and is not
        planned. That is what keeps a second plan empty.
        """
        assert unit.completed_at is not None  # _judge admits no run without one
        archive_ids = {archive.id for _entry, archive in members}
        own = next(
            (
                row
                for row in self._facts.log_rows
                if row.status == unit.status
                and row.created_at is not None
                and abs(row.created_at - unit.completed_at) <= TERMINAL_WINDOW
                and (row.printer_id == unit.printer_id or row.archive_id in archive_ids)
            ),
            None,
        )
        # The keeper, when one is kept: the row written on the run's own printer first. A row
        # another printer's replay wrote names the wrong machine.
        ordered = sorted(
            members,
            key=lambda m: (m[0].printer_id != unit.printer_id, m[0].created_at or datetime.min, m[0].id),
        )
        if own is not None:
            drops, reason = ordered, f"unit {unit.id}'s {unit.status} terminal is already recorded by entry {own.id}"
        else:
            keeper_entry, keeper_archive = ordered[0]
            rewrite = self._rewrite(keeper_entry, keeper_archive, unit)
            if rewrite.after != rewrite.before:
                self._actions.append(rewrite)
            drops, reason = ordered[1:], f"another replay of unit {unit.id}; entry {keeper_entry.id} carries the run"
        for entry, archive in drops:
            self._actions.append(
                DropPrintLogEntry(
                    entry_id=entry.id,
                    archive_id=archive.id,
                    unit_id=unit.id,
                    status=entry.status,
                    created_at=entry.created_at or datetime.min,
                    reason=reason,
                )
            )

    def _rewrite(self, entry: _LogRow, archive: _Archive, unit: _Unit) -> RewritePrintLogEntry:
        """The replay row, restated as the run's terminal.

        The replay's own grams and cost came from another print's progress. The best evidence of
        what the run consumed is its REAL terminal's own charges: the archive-less charges the
        chain wrote through the dispatch donor, on the run's printer, within its terminal window.
        Their sum is the run's grams, rounded like ``job_terminal.compute_run_filament_grams``'s tracked
        sum, and their cost sum is its cost, as the writer's per-run cost is. With no such
        charges, only a completed run has a known figure: the writer's own completed fallback,
        the archive's grams and cost. A failed or cancelled run's partial grams are then unknown
        (None), and its cost stays as written.
        """
        assert unit.completed_at is not None
        completed = unit.status == _COMPLETED
        duration = entry.duration_seconds
        if entry.started_at is not None and entry.started_at <= unit.completed_at:
            duration = int((unit.completed_at - entry.started_at).total_seconds())
        printer_id, printer_name = entry.printer_id, entry.printer_name
        if entry.printer_id != unit.printer_id:
            printer_id, printer_name = unit.printer_id, self._facts.printer_names.get(unit.printer_id)
        donors = self._facts.donor_charges(unit)
        grams: float | None
        cost: float | None
        if donors:
            grams = round(sum(donor.weight_used for donor in donors), 1)
            cost = sum(donor.cost or 0.0 for donor in donors) or None
        elif completed:
            grams, cost = archive.filament_used_grams, archive.cost
        else:
            grams, cost = None, entry.cost
        after = LogEntryFields(
            status=unit.status,
            completed_at=unit.completed_at,
            duration_seconds=duration,
            filament_used_grams=grams,
            cost=cost,
            failure_reason=None if completed else entry.failure_reason,
            printer_id=printer_id,
            printer_name=printer_name,
        )
        return RewritePrintLogEntry(
            entry_id=entry.id,
            archive_id=archive.id,
            unit_id=unit.id,
            created_at=entry.created_at or datetime.min,
            before=entry.fields(),
            after=after,
        )

    # R-charge -------------------------------------------------------------------------------------
    def _plan_charges(self) -> None:
        # (weight_used, weight_used_baseline) per spool as the planned reversals leave it, so a
        # second reversal on one spool is planned, and guarded, on the first one's after-image.
        states: dict[int, tuple[float, float]] = {}
        for charge in self._facts.charges:
            archive = self._facts.archives.get(charge.archive_id)
            if archive is None:
                self._count(f"charge: {_TALLY_ARCHIVE_GONE}")
                continue
            verdict = self._judge(archive, charge.created_at)
            if verdict.tally is not None:
                self._count(f"charge: {verdict.tally}")
                continue
            if verdict.skip is not None:
                self._skip("spool_usage_history", charge.id, archive.id, *verdict.skip)
                continue
            unit = verdict.unit
            if unit is None or unit.completed_at is None:  # _judge admits no replay without both
                continue
            spool = self._facts.spools.get(charge.spool_id)
            refusal = self._spool_refusal(spool)
            if refusal is not None or spool is None:  # the refusal names a missing spool too
                code, detail = refusal or ("spool_missing", "the spool row is gone")
                self._skips.append(
                    SkipSpoolCharge(
                        usage_id=charge.id,
                        spool_id=charge.spool_id,
                        archive_id=archive.id,
                        grams=charge.weight_used,
                        code=code,
                        detail=detail,
                    )
                )
                continue
            before, baseline_before = states.get(spool.id, (spool.weight_used, spool.weight_used_baseline))
            after = max(0.0, before - charge.weight_used)
            # Below the baseline means the operator's reset came after the phantom and anchored it.
            baseline_after = (
                max(0.0, baseline_before - charge.weight_used) if after < baseline_before else baseline_before
            )
            states[spool.id] = (after, baseline_after)
            self._actions.append(
                ReverseSpoolCharge(
                    usage_id=charge.id,
                    spool_id=spool.id,
                    archive_id=archive.id,
                    unit_id=unit.id,
                    run_status=unit.status,
                    run_completed_at=unit.completed_at,
                    charge_status=charge.status,
                    created_at=charge.created_at or datetime.min,
                    grams=charge.weight_used,
                    spool_weight_used_before=before,
                    spool_weight_used_after=after,
                    baseline_before=baseline_before,
                    baseline_after=baseline_after,
                )
            )

    @staticmethod
    def _spool_refusal(spool: _Spool | None) -> tuple[SpoolSkipCode, str] | None:
        """Why a spool's weight is not the repair's to move, or None when it is."""
        if spool is None:
            return ("spool_missing", "the spool row is gone")
        if spool.weight_locked:
            return ("weight_locked", "the operator locked this spool's weight")
        if spool.spent_at is not None:
            return ("spent", f"the spool was observed spent at {_fmt(spool.spent_at)}")
        if spool.archived_at is not None:
            return ("archived", f"the spool was archived at {_fmt(spool.archived_at)}")
        return None

    def _skip(self, table: SkippedTable, row_id: int, archive_id: int | None, code: RowSkipCode, detail: str) -> None:
        self._skips.append(SkipRow(table=table, row_id=row_id, archive_id=archive_id, code=code, detail=detail))


def plan_foreign_replay_repair(conn: Connection, *, now: datetime) -> RepairPlan:
    """Plan the repair from one read of the database. SELECTs only.

    ``now`` stamps the archives R-dup closes with no recorded outcome. A timezone-aware value is
    converted to the naive UTC the database stores.
    """
    now_utc = _naive_utc(now)
    assert now_utc is not None
    return _Planner(_Facts(conn), now_utc).plan()


# --- apply -------------------------------------------------------------------------------------------


def _expect_one(rowcount: int, action: RepairAction, statement: str) -> None:
    if rowcount != 1:
        raise RepairDrift(f"{statement} hit {rowcount} rows, expected 1: {describe(action)}")


def apply_foreign_replay_repair(conn: Connection, plan: RepairPlan) -> None:
    """Execute exactly ``plan.actions``, in order, each guarded on its pre-image.

    Order matters for spools: a second reversal on one spool is guarded on the first one's
    ``after``. Any statement that does not hit exactly one row raises ``RepairDrift``. The
    caller owns the transaction (a savepoint in the migration), so a raise discards every
    statement before it.
    """
    for action in plan.actions:
        _apply_one(conn, action)


def _apply_one(conn: Connection, action: RepairAction) -> None:
    match action:
        case ClosePrintingDuplicate():
            values: dict[str, object] = {"status": action.to_status, "completed_at": action.completed_at}
            if action.to_status == _COMPLETED:
                values["failure_reason"] = None
            _expect_one(
                conn.execute(
                    update(_archives)
                    .where(_archives.c.id == action.archive_id, _archives.c.status == _ARCHIVE_PRINTING)
                    .values(values)
                ).rowcount,
                action,
                "UPDATE print_archives",
            )
        case RestoreArchiveOutcome():
            _expect_one(
                conn.execute(
                    update(_archives)
                    .where(_archives.c.id == action.archive_id, _archives.c.status == action.from_status)
                    .values(
                        status=action.to_status,
                        completed_at=action.completed_at,
                        failure_reason=action.failure_reason,
                    )
                ).rowcount,
                action,
                "UPDATE print_archives",
            )
        case RewritePrintLogEntry():
            after = action.after
            _expect_one(
                conn.execute(
                    update(_log)
                    .where(_log.c.id == action.entry_id, _log.c.status == action.before.status)
                    .values(
                        status=after.status,
                        completed_at=after.completed_at,
                        duration_seconds=after.duration_seconds,
                        filament_used_grams=after.filament_used_grams,
                        cost=after.cost,
                        failure_reason=after.failure_reason,
                        printer_id=after.printer_id,
                        printer_name=after.printer_name,
                    )
                ).rowcount,
                action,
                "UPDATE print_log_entries",
            )
        case DropPrintLogEntry():
            _expect_one(
                conn.execute(delete(_log).where(_log.c.id == action.entry_id, _log.c.status == action.status)).rowcount,
                action,
                "DELETE print_log_entries",
            )
        case ReverseSpoolCharge():
            _expect_one(
                conn.execute(
                    update(_spools)
                    .where(
                        _spools.c.id == action.spool_id,
                        func.abs(func.coalesce(_spools.c.weight_used, 0.0) - action.spool_weight_used_before)
                        < _WEIGHT_EPSILON,
                        func.abs(func.coalesce(_spools.c.weight_used_baseline, 0.0) - action.baseline_before)
                        < _WEIGHT_EPSILON,
                        _spools.c.weight_locked.is_not(True),
                        _spools.c.spent_at.is_(None),
                        _spools.c.archived_at.is_(None),
                    )
                    .values(weight_used=action.spool_weight_used_after, weight_used_baseline=action.baseline_after)
                ).rowcount,
                action,
                "UPDATE spool",
            )
            _expect_one(
                conn.execute(
                    delete(_charges).where(
                        _charges.c.id == action.usage_id,
                        _charges.c.spool_id == action.spool_id,
                        _charges.c.archive_id == action.archive_id,
                        _charges.c.status == action.charge_status,
                    )
                ).rowcount,
                action,
                "DELETE spool_usage_history",
            )
        case _:
            raise TypeError(f"not a repair action: {action!r}")


# --- the report (ASCII only: the farm PC's console is cp1252) ---------------------------------------


def _fmt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value is not None else "-"


def _grams(value: float | None) -> str:
    return f"{value:.1f} g" if value is not None else "-"


def _q(value: str | None) -> str:
    """A database string, quoted and escaped to ASCII: operators name printers in any script."""
    return ascii(value)


def _log_fields(fields: LogEntryFields) -> str:
    return (
        f"{fields.status} completed={_fmt(fields.completed_at)} duration={fields.duration_seconds} "
        f"grams={_grams(fields.filament_used_grams)} cost={fields.cost} failure={_q(fields.failure_reason)} "
        f"printer={fields.printer_id}/{_q(fields.printer_name)}"
    )


def describe(item: RepairAction | RepairSkip) -> str:
    """One line per action or skip, with the before-values: the report's body and the migration's log."""
    match item:
        case ClosePrintingDuplicate():
            return (
                f"close-duplicate archive {item.archive_id} printer {item.printer_id}: printing since "
                f"{_fmt(item.started_at)} (failure={_q(item.failure_reason_before)}) -> {item.to_status} at "
                f"{_fmt(item.completed_at)} [unit {item.unit_id}; printer keeps printing archive {item.kept_archive_id}]"
            )
        case RestoreArchiveOutcome():
            return (
                f"restore-archive {item.archive_id}: {item.from_status} at {_fmt(item.completed_at_before)} "
                f"(failure={_q(item.failure_reason_before)}) -> {item.to_status} at {_fmt(item.completed_at)} "
                f"(failure={_q(item.failure_reason)}) [unit {item.unit_id}]"
            )
        case RewritePrintLogEntry():
            return (
                f"rewrite-log entry {item.entry_id} archive {item.archive_id} written {_fmt(item.created_at)}: "
                f"{_log_fields(item.before)} -> {_log_fields(item.after)} [unit {item.unit_id}]"
            )
        case DropPrintLogEntry():
            return (
                f"drop-log entry {item.entry_id} archive {item.archive_id}: {item.status} written "
                f"{_fmt(item.created_at)} -- {ascii(item.reason)[1:-1]}"
            )
        case ReverseSpoolCharge():
            baseline = (
                f"baseline {item.baseline_before:.1f} -> {item.baseline_after:.1f} (a reset after the phantom anchored it)"
                if item.baseline_after != item.baseline_before
                else f"baseline {item.baseline_before:.1f} kept"
            )
            return (
                f"reverse-charge usage {item.usage_id} spool {item.spool_id} archive {item.archive_id}: "
                f"{_grams(item.grams)} {item.charge_status} written {_fmt(item.created_at)}; spool weight_used "
                f"{item.spool_weight_used_before:.1f} -> {item.spool_weight_used_after:.1f}, {baseline} "
                f"[unit {item.unit_id} {item.run_status} at {_fmt(item.run_completed_at)}]"
            )
        case SkipSpoolCharge():
            return (
                f"skip-charge usage {item.usage_id} spool {item.spool_id} archive {item.archive_id}: "
                f"{_grams(item.grams)} left in place -- {item.code}: {ascii(item.detail)[1:-1]}"
            )
        case SkipRow():
            return (
                f"skip {item.table} {item.row_id} archive {item.archive_id} -- {item.code}: {ascii(item.detail)[1:-1]}"
            )
        case _:
            raise TypeError(f"not a repair action or skip: {item!r}")


_ACTION_LABELS: Final[tuple[tuple[type[RepairAction], str], ...]] = (
    (ClosePrintingDuplicate, "close printing duplicate (R-dup)"),
    (RestoreArchiveOutcome, "restore archive outcome (R-archive)"),
    (RewritePrintLogEntry, "rewrite print-log entry (R-log)"),
    (DropPrintLogEntry, "drop print-log entry (R-log)"),
    (ReverseSpoolCharge, "reverse spool charge (R-charge)"),
)


def _count_lines(items: Sequence[RepairAction]) -> list[str]:
    lines = []
    for kind, label in _ACTION_LABELS:
        count = sum(1 for item in items if isinstance(item, kind))
        lines.append(f"  {label}: {count}")
    reversals = [item for item in items if isinstance(item, ReverseSpoolCharge)]
    if reversals:
        grams = sum(item.grams for item in reversals)
        spools = len({item.spool_id for item in reversals})
        lines.append(f"  grams reversed: {grams:.1f} g across {spools} spool(s)")
    return lines


def format_report(plan: RepairPlan) -> str:
    """Counts, then one line per action and per skip, each with its before-values. ASCII only."""
    lines = [f"foreign-replay repair plan (now {_fmt(plan.now)} UTC; all times UTC)", f"actions: {len(plan.actions)}"]
    lines.extend(_count_lines(plan.actions))
    skip_counts: dict[str, int] = defaultdict(int)
    for skip in plan.skips:
        key = f"charge {skip.code}" if isinstance(skip, SkipSpoolCharge) else f"{skip.table} {skip.code}"
        skip_counts[key] += 1
    lines.append(f"skipped: {len(plan.skips)}")
    lines.extend(f"  {key}: {count}" for key, count in sorted(skip_counts.items()))
    lines.append("examined and left alone:")
    lines.extend(f"  {label}: {count}" for label, count in plan.tallies)
    lines.append("")
    lines.append("ACTIONS")
    lines.extend(f"  {describe(action)}" for action in plan.actions)
    lines.append("")
    lines.append("SKIPPED")
    lines.extend(f"  {describe(skip)}" for skip in plan.skips)
    return "\n".join(lines) + "\n"
