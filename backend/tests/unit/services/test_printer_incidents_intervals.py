"""The incident ledger read as INTERVALS — ``list_overlapping`` / ``held_seconds`` / ``held_stats``.

``printer_incident`` is a durable interval ledger (``created_at`` → ``resolved_at``,
NULL = still holding), and a downtime figure is a sum over those intervals clipped
to a window. ``list_recent`` cannot serve that: it filters ``created_at >= since``,
so an incident that opened before the window and held right through it — the longest
outages, the ones a downtime figure least may miss — is invisible to it. These pins
hold the overlap test at both boundaries, the absence of a row cap, and the one
derivation of a hold's duration that ``GET /incidents`` and any overlay share.
"""

from datetime import datetime, timedelta

import pytest

from backend.app.models.printer_incident import (
    KIND_JAM,
    KIND_PHYSICAL,
    KIND_POWER_LOSS,
    KIND_SERVICE_HOLD,
    STATUS_ESCALATED,
    STATUS_RESOLVED,
    PrinterIncident,
)
from backend.app.services import printer_incidents

# No module-level asyncio mark: the file mixes the DB-backed query pins with the
# pure ones, and ``asyncio_mode = "auto"`` already runs the async half.

_START = datetime(2026, 3, 10, 12, 0, 0)
_END = datetime(2026, 3, 10, 18, 0, 0)


def _incident(
    *,
    printer_id: int = 1,
    kind: str = KIND_JAM,
    created_at: datetime | None = _START,
    resolved_at: datetime | None = None,
    job_id: str = "",
) -> PrinterIncident:
    """A row, built directly — these helpers read columns, not the open/close machine."""
    return PrinterIncident(
        printer_id=printer_id,
        job_id=job_id,
        item_id=None,
        kind=kind,
        code="",
        codes="",
        slot_global_tray=None,
        status=STATUS_RESOLVED if resolved_at else STATUS_ESCALATED,
        created_at=created_at,
        resolved_at=resolved_at,
    )


class TestListOverlapping:
    """Every row whose ``[created_at, resolved_at or +inf)`` meets ``[start, end)``."""

    @pytest.fixture
    async def seeded(self, db_session, printer_factory) -> None:
        a = await printer_factory()
        b = await printer_factory()
        day = _START.date()

        def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
            return datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute, second=second)

        db_session.add_all(
            [
                # Entirely before the window.
                _incident(printer_id=a.id, created_at=at(8), resolved_at=at(9), job_id="before"),
                # Entirely after it.
                _incident(printer_id=a.id, created_at=at(19), resolved_at=at(20), job_id="after"),
                # Wholly inside.
                _incident(printer_id=a.id, created_at=at(13), resolved_at=at(14), job_id="inside"),
                # Opened before, closed after — the row list_recent cannot see.
                _incident(printer_id=a.id, created_at=at(6), resolved_at=at(23), job_id="spanning"),
                # Still open, opened long before the window.
                _incident(printer_id=a.id, kind=KIND_POWER_LOSS, created_at=at(6), job_id="open_ended"),
                # Still open, but opened after the window closed.
                _incident(printer_id=b.id, kind=KIND_POWER_LOSS, created_at=at(19), job_id="open_after_end"),
                # Closed at the exact instant the window opens.
                _incident(printer_id=a.id, created_at=at(8), resolved_at=_START, job_id="touch_start"),
                # Opened at the exact instant the window closes.
                _incident(printer_id=a.id, created_at=_END, resolved_at=at(19), job_id="touch_end"),
                # One second the other side of each boundary.
                _incident(printer_id=a.id, created_at=at(8), resolved_at=at(12, 0, 1), job_id="just_after_start"),
                _incident(printer_id=a.id, created_at=at(17, 59, 59), resolved_at=at(20), job_id="just_before_end"),
            ]
        )
        await db_session.commit()

    async def test_only_the_intersecting_rows_come_back_oldest_first(self, db_session, seeded):
        rows = await printer_incidents.list_overlapping(db_session, start=_START, end=_END)

        assert [row.job_id for row in rows] == [
            "spanning",
            "open_ended",
            "just_after_start",
            "inside",
            "just_before_end",
        ]

    async def test_the_window_is_half_open_at_both_ends(self, db_session, seeded):
        """A row that ends exactly AT ``start``, or begins exactly AT ``end``, is out.

        Half-open intervals are what make adjacent windows tile without
        double-counting a boundary second — the property a bucketed timeline
        depends on.
        """
        rows = await printer_incidents.list_overlapping(db_session, start=_START, end=_END)

        assert "touch_start" not in [row.job_id for row in rows]
        assert "touch_end" not in [row.job_id for row in rows]

    async def test_an_open_row_that_began_after_the_window_is_out(self, db_session, seeded):
        """Open means "no end", not "every window": its interval still STARTS somewhere."""
        rows = await printer_incidents.list_overlapping(db_session, start=_START, end=_END)

        assert "open_after_end" not in [row.job_id for row in rows]

    async def test_there_is_no_row_cap(self, db_session, printer_factory):
        """A cap on a set the caller is about to SUM would silently under-report."""
        printer = await printer_factory()
        db_session.add_all(
            [
                _incident(
                    printer_id=printer.id,
                    created_at=_START + timedelta(seconds=index),
                    resolved_at=_END - timedelta(seconds=1),
                )
                for index in range(250)
            ]
        )
        await db_session.commit()

        rows = await printer_incidents.list_overlapping(db_session, start=_START, end=_END)

        assert len(rows) == 250

    async def test_an_empty_window_answers_empty(self, db_session, seeded):
        rows = await printer_incidents.list_overlapping(
            db_session, start=datetime(2026, 3, 1), end=datetime(2026, 3, 2)
        )

        assert rows == []


class TestHeldSeconds:
    """Parity with the inline derivation this lifted out of ``routes/incidents._row``."""

    @staticmethod
    def _inline(incident: PrinterIncident, now: datetime) -> float:
        """The route's former arithmetic, verbatim, as the oracle."""
        end = incident.resolved_at or now
        return max(0.0, (end - incident.created_at).total_seconds()) if incident.created_at else 0.0

    @pytest.mark.parametrize(
        "incident",
        [
            _incident(created_at=_START, resolved_at=_START + timedelta(seconds=90)),
            _incident(created_at=_START, resolved_at=None),
            # A clock step between two utcnow() calls: a close stamped before the open.
            _incident(created_at=_START, resolved_at=_START - timedelta(seconds=5)),
            # Closed at the same instant it opened.
            _incident(created_at=_START, resolved_at=_START),
            # Never flushed, so the non-nullable column is still unset.
            _incident(created_at=None),
        ],
    )
    def test_matches_the_lifted_arithmetic(self, incident: PrinterIncident):
        now = _END

        assert printer_incidents.held_seconds(incident, now) == self._inline(incident, now)

    def test_an_open_row_is_measured_up_to_now(self):
        incident = _incident(created_at=_START, resolved_at=None)

        assert printer_incidents.held_seconds(incident, _START + timedelta(minutes=30)) == 1800.0

    def test_a_backwards_close_never_reads_as_negative_downtime(self):
        incident = _incident(created_at=_START, resolved_at=_START - timedelta(seconds=5))

        assert printer_incidents.held_seconds(incident, _END) == 0.0

    def test_a_row_with_no_created_at_is_zero_rather_than_a_crash(self):
        assert printer_incidents.held_seconds(_incident(created_at=None), _END) == 0.0


class TestHeldStats:
    """Per-kind hold time. The OUTCOME tally stays ``summary``'s — this owns durations."""

    def test_mixed_kinds_with_open_rows(self):
        now = _START + timedelta(hours=1)
        rows = [
            # jam: ten closed recoveries, 10 s .. 100 s, plus one still holding.
            *[
                _incident(kind=KIND_JAM, created_at=_START, resolved_at=_START + timedelta(seconds=seconds))
                for seconds in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)
            ],
            _incident(kind=KIND_JAM, created_at=now - timedelta(seconds=5), resolved_at=None),
            # physical: one closed row.
            _incident(kind=KIND_PHYSICAL, created_at=_START, resolved_at=_START + timedelta(seconds=42)),
            # power_loss: open only — nothing has recovered yet.
            _incident(kind=KIND_POWER_LOSS, created_at=now - timedelta(seconds=120), resolved_at=None),
        ]

        stats = printer_incidents.held_stats(rows, now)

        jam = stats[KIND_JAM]
        assert jam.count == 11
        assert jam.open_count == 1
        # 10+20+…+100 = 550, plus the open row clamped at now.
        assert jam.total_held_s == 555.0
        assert jam.median_recover_s == 55.0
        assert jam.p90_recover_s == 90.0

        physical = stats[KIND_PHYSICAL]
        assert physical.count == 1
        assert physical.open_count == 0
        assert physical.median_recover_s == 42.0
        # Nearest rank is total at n=1 — the reason it is the method.
        assert physical.p90_recover_s == 42.0

        power_loss = stats[KIND_POWER_LOSS]
        assert power_loss.count == 1
        assert power_loss.open_count == 1
        assert power_loss.total_held_s == 120.0
        # Time to recover is unknown while the printer is still down.
        assert power_loss.median_recover_s is None
        assert power_loss.p90_recover_s is None

    def test_the_percentile_methods_are_pinned(self):
        """Median interpolates on an even count; p90 is NEAREST-RANK and never does.

        Two closed recoveries of 10 s and 20 s: the median is the textbook 15 s
        (no such recovery happened, and that is what a median means), while p90
        answers 20 s — a duration that was actually measured.
        """
        rows = [
            _incident(created_at=_START, resolved_at=_START + timedelta(seconds=10)),
            _incident(created_at=_START, resolved_at=_START + timedelta(seconds=20)),
        ]

        stats = printer_incidents.held_stats(rows, _END)[KIND_JAM]

        assert stats.median_recover_s == 15.0
        assert stats.p90_recover_s == 20.0

    def test_a_declared_hold_is_counted_like_any_other_kind(self):
        """A maintenance hold holds the printer for real seconds, so it gets a row.

        ``summary`` keeps declared rows out of its fault tally and inside
        ``by_kind``; this is the same reach, and a caller that wants equipment
        faults alone filters on ``FAULT_KINDS`` before calling.
        """
        rows = [
            _incident(kind=KIND_SERVICE_HOLD, created_at=_START, resolved_at=_START + timedelta(hours=2)),
            _incident(kind=KIND_JAM, created_at=_START, resolved_at=_START + timedelta(seconds=30)),
        ]

        stats = printer_incidents.held_stats(rows, _END)

        assert set(stats) == {KIND_SERVICE_HOLD, KIND_JAM}
        assert stats[KIND_SERVICE_HOLD].total_held_s == 7200.0

    def test_no_rows_is_an_empty_mapping(self):
        assert printer_incidents.held_stats([], _END) == {}

    def test_the_result_is_frozen(self):
        stats = printer_incidents.held_stats([_incident(created_at=_START, resolved_at=_END)], _END)

        with pytest.raises(AttributeError):
            stats[KIND_JAM].count = 99
