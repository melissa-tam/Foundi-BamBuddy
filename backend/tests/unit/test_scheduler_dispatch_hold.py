"""Regression tests for the post-dispatch hold (#1157) — the occupancy LEASE.

When the scheduler dispatches a print it claims the printer, so a second dispatch cannot land on
the same machine while the first start is still landing. This is defense in depth alongside the DB
``busy_printers`` seed.

Why it exists: on the H2D Pro, ``project_file`` can take 80-210 s to show. During that window users
were getting 3 plates of the same multi-plate file dispatched 30 s apart onto the same printer.

What ENDS the hold changed on 2026-10-11 (owner 9 of the event-not-clock plan): a committed lease
holds until the dispatch's START is answered by the record (``services/dispatch_start``: reached,
refused, void, taken over) — never a minimum cooldown or a hard ceiling. The 60 s / 180 s bounds the
scheduler used to contribute guessed the outcome from a clock: a start still digesting at 180 s let
a second dispatch onto the printer. These tests drive ``plate_occupancy._now_mono`` to prove no window
settles a lease, and ask through ``dispatchable`` / ``snapshot``, exactly as the scheduler asks.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.app.services import plate_occupancy as po
from backend.app.services.plate_occupancy import DispatchLease, Evidence, plate_occupancy
from backend.app.services.print_scheduler import PrintScheduler


@pytest.fixture(autouse=True)
def _clean_authority():
    """Every test starts with an empty fleet and un-wired callables."""
    plate_occupancy.reset_for_tests()
    yield
    plate_occupancy.reset_for_tests()


@pytest.fixture
def clock(monkeypatch):
    """Drive the authority's monotonic clock by hand — to prove no window ends a lease."""
    now = {"t": 1_000.0}
    monkeypatch.setattr(po, "_now_mono", lambda: now["t"])
    return now


def _status(state: str | None, subtask_id: str | None = None):
    return SimpleNamespace(state=state, subtask_id=subtask_id, gcode_file=None)


def _claim(sched: PrintScheduler, printer_id: int, unit_id: int, *, status) -> DispatchLease:
    """Mint the lease exactly as the scheduler's plan step does."""
    with patch("backend.app.services.print_scheduler.printer_manager.get_status", MagicMock(return_value=status)):
        lease = sched._claim_dispatch_lease(printer_id, unit_id)
    assert isinstance(lease, DispatchLease), f"claim refused: {lease!r}"
    return lease


def _held(printer_id: int, live_state: str | None) -> bool:
    """Does the authority still hold this printer for a dispatch, given the live wire?"""
    return plate_occupancy.dispatchable(printer_id, Evidence(live_state=live_state)) == "dispatch_in_flight"


class TestDispatchLeaseHoldsThePrinter:
    def test_held_immediately_after_commit(self, clock):
        sched = PrintScheduler()
        lease = _claim(sched, 42, 1, status=_status("FINISH", "subtask-1"))
        assert plate_occupancy.commit_dispatch(42, lease) is None
        assert _held(42, "FINISH") is True

    def test_unclaimed_printer_not_held(self, clock):
        assert _held(42, "FINISH") is False
        assert plate_occupancy.dispatchable(42, Evidence(live_state="IDLE")) is None

    @pytest.mark.parametrize("live_state", ["FINISH", "IDLE", "PREPARE", None, ""])
    def test_slow_firmware_keeps_the_hold_until_the_start_is_answered(self, clock, live_state):
        """An H2D digesting a project_file for 210 s, or 30 min: the hold stands. ``PREPARE`` is
        refused as an active job anyway; the point is that no clock lets the lease lapse."""
        sched = PrintScheduler()
        lease = _claim(sched, 42, 1, status=_status("FINISH", "subtask-1"))
        plate_occupancy.commit_dispatch(42, lease)

        clock["t"] += 30 * 60
        assert plate_occupancy.snapshot(42, Evidence(live_state=live_state)).lease_active is True
        assert plate_occupancy.printers_with_lease_or_eject() == {42}

    def test_an_uncommitted_lease_always_holds(self, clock):
        sched = PrintScheduler()
        _claim(sched, 42, 1, status=_status("FINISH", "subtask-1"))
        clock["t"] += 10_000
        assert _held(42, "FINISH") is True


class TestDispatchLeaseReleases:
    def test_release_on_the_starts_answer(self, clock):
        sched = PrintScheduler()
        lease = _claim(sched, 42, 1, status=_status("FINISH", "subtask-1"))
        plate_occupancy.commit_dispatch(42, lease)

        plate_occupancy.release_dispatch(42, "the start was reached (action 9)")
        assert _held(42, "FINISH") is False

    def test_release_is_idempotent_and_never_raises(self, clock):
        sched = PrintScheduler()
        plate_occupancy.release_dispatch(99999, "not held")  # must not raise

        lease = _claim(sched, 99999, 7, status=_status("FINISH", "subtask-1"))
        plate_occupancy.commit_dispatch(99999, lease)
        assert plate_occupancy.snapshot(99999).lease_unit_id == 7

        plate_occupancy.release_dispatch(99999, "the start was void (action 9)")
        assert plate_occupancy.snapshot(99999).lease_unit_id is None
        plate_occupancy.release_dispatch(99999, "the start was void (action 9)")
        assert plate_occupancy.snapshot(99999).lease_unit_id is None


class TestPerPrinterIsolation:
    def test_hold_does_not_leak_across_printers(self, clock):
        sched = PrintScheduler()
        lease = _claim(sched, 42, 1, status=_status("FINISH", "subtask-1"))
        plate_occupancy.commit_dispatch(42, lease)

        assert _held(99, "FINISH") is False
        assert _held(42, "FINISH") is True


class TestTheClaim:
    def test_the_scheduler_contributes_no_clock_to_the_lease(self, clock):
        """The 60 s minimum cooldown and the 180 s ceiling are gone (2026-10-11)."""
        sched = PrintScheduler()
        assert not hasattr(sched, "_dispatch_min_cooldown")
        assert not hasattr(sched, "_dispatch_max_hold")

        lease = _claim(sched, 42, 1, status=_status("FINISH", "subtask-1"))
        assert lease.unit_id == 1
        assert not hasattr(lease, "min_hold_s")
        assert not hasattr(lease, "max_hold_s")

    def test_a_claim_is_refused_when_the_printer_is_already_claimed(self, clock):
        """The claim is gated by exactly ``dispatchable``; the helper hands back the authority's own
        refusal TOKEN so the unwinding caller can say WHY."""
        sched = PrintScheduler()
        lease = _claim(sched, 42, 1, status=_status("FINISH", "subtask-1"))
        plate_occupancy.commit_dispatch(42, lease)

        with patch(
            "backend.app.services.print_scheduler.printer_manager.get_status",
            MagicMock(return_value=_status("FINISH", "subtask-1")),
        ):
            refused = sched._claim_dispatch_lease(42, 2)
        assert not isinstance(refused, DispatchLease)
        assert refused == "dispatch_in_flight"
