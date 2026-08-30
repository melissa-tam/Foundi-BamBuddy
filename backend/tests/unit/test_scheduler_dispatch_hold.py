"""Regression tests for the post-dispatch hold (#1157) — now the occupancy LEASE.

When the scheduler dispatches a print it claims the printer, so a second dispatch
cannot land on the same machine until either the printer transitions out of its
pre-dispatch state OR a hard timeout expires. This is defense in depth alongside
the DB ``busy_printers`` seed.

Why it exists: on the H2D Pro, ``project_file`` ack can take 80-210 s. During that
window users were getting 3 plates of the same multi-plate file dispatched 30 s
apart onto the same printer — the seed query was empirically missing in-flight
items even though the queue items were marked ``printing`` in the DB. The hold
removes the dependency on DB-row visibility / completion-callback timing.

The MECHANISM moved on 2026-08-30: ``PrintScheduler._dispatch_holds`` (with
``_mark_printer_dispatched`` / ``_release_dispatch_hold`` /
``_printer_in_dispatch_hold``) was one of the five stores answering "who owns this
printer", and it is now ``plate_occupancy``'s :class:`DispatchLease` — one of the
authority's three stored facts. The SEMANTICS are unchanged and are what this file
pins:

* an UNCOMMITTED lease (the upload window) always holds;
* a COMMITTED lease holds until the wire moves off ``pre_state`` AND ``min_hold_s``
  has elapsed, or until ``max_hold_s`` expires outright;
* a falsy ``pre_state`` means no transition is observable, so ``min_hold_s`` alone
  decides.

Two consequences of the move are pinned explicitly rather than assumed. The bounds
still come from the scheduler (``_dispatch_min_cooldown`` / ``_dispatch_max_hold``),
which is what makes them tunable from here. And settlement is a READ-time function
inside the authority, on ``time.monotonic`` — so these tests drive
``plate_occupancy._now_mono`` instead of sleeping, and ask through
``dispatchable`` / ``snapshot``, which is exactly how the scheduler asks.
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
    """Drive the authority's monotonic hold clock by hand.

    The lease window is measured on ``time.monotonic`` (never wall time — an NTP
    step must not settle or extend a hold), so a test that wants to be past a
    deadline moves the clock rather than sleeping through it.
    """
    now = {"t": 1_000.0}
    monkeypatch.setattr(po, "_now_mono", lambda: now["t"])
    return now


def _status(state: str | None, subtask_id: str | None = None):
    return SimpleNamespace(state=state, subtask_id=subtask_id, gcode_file=None)


def _claim(sched: PrintScheduler, printer_id: int, unit_id: int, *, status) -> DispatchLease:
    """Mint the lease exactly as the scheduler's plan step does (wire snapshot and all)."""
    with patch("backend.app.services.print_scheduler.printer_manager.get_status", MagicMock(return_value=status)):
        lease = sched._claim_dispatch_lease(printer_id, unit_id)
    assert isinstance(lease, DispatchLease), f"claim refused: {lease!r}"
    return lease


def _held(printer_id: int, live_state: str | None) -> bool:
    """Does the authority still hold this printer for a dispatch, given the live wire?"""
    return plate_occupancy.dispatchable(printer_id, Evidence(live_state=live_state)) == "dispatch_in_flight"


class TestDispatchLeaseHoldsThePrinter:
    """A printer that just received a project_file is locked out of new
    dispatches until something releases it."""

    def test_held_immediately_after_commit(self, clock):
        sched = PrintScheduler()
        lease = _claim(sched, 42, 1, status=_status("FINISH", "subtask-1"))
        assert plate_occupancy.commit_dispatch(42, lease) is None
        assert _held(42, "FINISH") is True

    def test_unclaimed_printer_not_held(self, clock):
        assert _held(42, "FINISH") is False
        assert plate_occupancy.dispatchable(42, Evidence(live_state="IDLE")) is None

    def test_state_unchanged_keeps_hold(self, clock):
        """Printer still reporting pre_state ⇒ held.

        This is the main scenario: H2D Pro at FINISH for ~80 s after
        ``project_file``; the scheduler must not double-dispatch into that window.
        """
        sched = PrintScheduler()
        lease = _claim(sched, 42, 1, status=_status("FINISH", "subtask-1"))
        plate_occupancy.commit_dispatch(42, lease)
        clock["t"] += sched._dispatch_min_cooldown + 1  # cooldown alone is not enough
        assert _held(42, "FINISH") is True

    def test_an_uncommitted_lease_always_holds(self, clock):
        """The upload window. A claim is minted at PLAN time, seconds before the print
        command; there is nothing on the wire to transition yet, and the printer is
        unambiguously spoken for — no elapsed time may settle it."""
        sched = PrintScheduler()
        _claim(sched, 42, 1, status=_status("FINISH", "subtask-1"))
        clock["t"] += sched._dispatch_max_hold * 10
        assert _held(42, "PREPARE") is True
        assert plate_occupancy.snapshot(42).lease_unit_id == 1


class TestDispatchLeaseReleases:
    """The hold must release once the printer has actually picked up the job,
    so the next pending item for this printer can dispatch normally."""

    def test_release_via_explicit_call(self, clock):
        sched = PrintScheduler()
        lease = _claim(sched, 42, 1, status=_status("FINISH", "subtask-1"))
        plate_occupancy.commit_dispatch(42, lease)
        plate_occupancy.release_dispatch(42, "test")
        assert _held(42, "FINISH") is False
        assert plate_occupancy.snapshot(42).lease_unit_id is None

    def test_release_is_idempotent(self, clock):
        plate_occupancy.release_dispatch(42, "never claimed")  # never claimed
        plate_occupancy.release_dispatch(42, "double release")  # double-release
        assert plate_occupancy.snapshot(42).lease_unit_id is None
        assert _held(42, "FINISH") is False

    def test_state_transition_after_min_cooldown_releases(self, clock):
        """If the printer transitions away from pre_state AND the minimum cooldown
        has elapsed, the hold drops on the next read — and the spent lease is pruned
        rather than left to be re-asked about."""
        sched = PrintScheduler()
        sched._dispatch_min_cooldown = 0.0  # skip the cooldown floor for this test
        lease = _claim(sched, 42, 1, status=_status("FINISH", "subtask-1"))
        plate_occupancy.commit_dispatch(42, lease)

        assert _held(42, "PREPARE") is False
        assert plate_occupancy.snapshot(42).lease_unit_id is None

    def test_a_subtask_advance_alone_no_longer_settles_the_lease(self, clock):
        """CONTRACT (2026-08-30, deliberate narrowing): the STATE transition is the
        only settlement signal the authority has.

        The old in-scheduler hold released on either limb — a state change or a
        ``subtask_id`` advance past ``pre_subtask_id``. The authority carries
        ``pre_subtask`` on the lease but has no subtask evidence to compare it
        against (``Evidence`` is ``live_state`` + ``db_claim``), so an advance with an
        UNCHANGED state now keeps the hold until the ceiling. The subtask limb is not
        gone from the farm: it still exits Phase A of ``_watchdog_print_start``, which
        releases the lease itself on the active-state transition that follows. Holding
        a few seconds longer is the safe direction — under-reporting a claim sends a
        second dispatch onto a printer that is already taking a job.
        """
        sched = PrintScheduler()
        sched._dispatch_min_cooldown = 0.0
        lease = _claim(sched, 42, 1, status=_status("FINISH", "old-subtask-1"))
        plate_occupancy.commit_dispatch(42, lease)
        assert lease.pre_subtask == "old-subtask-1"

        # The printer echoed a NEW subtask id but has not left FINISH.
        assert _held(42, "FINISH") is True
        # The state transition that follows it does settle the lease.
        assert _held(42, "PREPARE") is False

    def test_transition_within_cooldown_still_holds(self, clock):
        """Even after a state transition, hold for at least min_cooldown so a slow
        printer that briefly pulses through PREPARE→RUNNING→PREPARE doesn't open a
        window for double-dispatch."""
        sched = PrintScheduler()
        sched._dispatch_min_cooldown = 60.0
        lease = _claim(sched, 42, 1, status=_status("FINISH", "subtask-1"))
        plate_occupancy.commit_dispatch(42, lease)
        clock["t"] += 59.0  # cooldown not elapsed → still held despite the transition
        assert _held(42, "PREPARE") is True
        clock["t"] += 2.0
        assert _held(42, "PREPARE") is False


class TestDispatchLeaseHardTimeout:
    """A lost MQTT session must not lock a printer out of the queue forever."""

    def test_hard_timeout_drops_hold(self, clock):
        sched = PrintScheduler()
        lease = _claim(sched, 42, 1, status=_status("FINISH", "subtask-1"))
        plate_occupancy.commit_dispatch(42, lease)
        assert _held(42, "FINISH") is True

        clock["t"] += sched._dispatch_max_hold + 1
        assert _held(42, "FINISH") is False
        assert plate_occupancy.snapshot(42).lease_unit_id is None


class TestDispatchLeaseFallbacks:
    """Edge cases around missing pre-dispatch data."""

    def test_no_pre_state_falls_back_to_time_only_hold(self, clock):
        """If the printer was disconnected at dispatch time we have no pre_state to
        compare against. Hold for the minimum cooldown anyway — better than allowing
        an immediate second dispatch onto a printer we couldn't even read state
        from."""
        sched = PrintScheduler()
        sched._dispatch_min_cooldown = 60.0
        lease = _claim(sched, 42, 1, status=None)
        assert lease.pre_state is None
        plate_occupancy.commit_dispatch(42, lease)

        # The live state doesn't matter — there's no pre_state to compare.
        assert _held(42, "RUNNING") is True

    def test_no_pre_state_releases_after_cooldown(self, clock):
        sched = PrintScheduler()
        sched._dispatch_min_cooldown = 60.0
        lease = _claim(sched, 42, 1, status=None)
        plate_occupancy.commit_dispatch(42, lease)

        clock["t"] += 61.0
        assert _held(42, "RUNNING") is False

    def test_status_unavailable_keeps_hold(self, clock):
        """If the printer disconnects after dispatch we can't read state — keep the
        hold until the hard timeout. Silence is not a transition, because releasing on
        missing data would let a second dispatch land on a printer we have no
        visibility into."""
        sched = PrintScheduler()
        lease = _claim(sched, 42, 1, status=_status("FINISH", "subtask-1"))
        plate_occupancy.commit_dispatch(42, lease)
        clock["t"] += sched._dispatch_min_cooldown + 1

        assert _held(42, None) is True  # no live state at all
        assert _held(42, "") is True  # an empty echo is silence too


class TestPerPrinterIsolation:
    """Holds on one printer must not affect another."""

    def test_hold_does_not_leak_across_printers(self, clock):
        sched = PrintScheduler()
        lease = _claim(sched, 42, 1, status=_status("FINISH", "subtask-1"))
        plate_occupancy.commit_dispatch(42, lease)

        # Printer 99 was never dispatched-to — must not be held.
        assert _held(99, "FINISH") is False
        # Printer 42 still held.
        assert _held(42, "FINISH") is True


class TestLeaseBoundsComeFromTheScheduler:
    """The two numbers are still the scheduler's contribution to the authority's
    lease — tunable in one place, and carried onto the record it mints."""

    def test_the_claim_carries_the_schedulers_bounds(self, clock):
        sched = PrintScheduler()
        assert sched._dispatch_min_cooldown == 60.0
        assert sched._dispatch_max_hold == 180.0

        lease = _claim(sched, 42, 1, status=_status("FINISH", "subtask-1"))
        assert lease.min_hold_s == sched._dispatch_min_cooldown
        assert lease.max_hold_s == sched._dispatch_max_hold
        assert lease.unit_id == 1
        assert lease.pre_state == "FINISH"

    def test_a_claim_is_refused_when_the_printer_is_already_claimed(self, clock):
        """The claim is gated by exactly ``dispatchable``, so the check that picks a
        printer and the one that mints the claim cannot drift apart.

        The helper hands back the authority's own refusal TOKEN rather than a bare
        None, so the caller that unwinds can say WHY — "the plate is occupied" and
        "a dispatch is already in flight" unwind identically but read very differently
        in the line that reports it.
        """
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


class TestWatchdogIntegration:
    """The start watchdog drops the claim on its happy paths so the next pending
    item can dispatch immediately. Without this, a successful print leaves the
    printer claimed until the hard timeout — blocking valid follow-up dispatches."""

    def test_release_dispatch_is_reachable_and_idempotent_on_the_authority(self, clock):
        """``_watchdog_print_start`` is a staticmethod calling
        ``plate_occupancy.release_dispatch(printer_id, "dispatch watchdog")`` on every
        exit. Smoke-test that surface: it must never raise, held or not."""
        sched = PrintScheduler()

        plate_occupancy.release_dispatch(99999, "dispatch watchdog")  # not held — must not raise

        lease = _claim(sched, 99999, 7, status=_status("FINISH", "subtask-1"))
        plate_occupancy.commit_dispatch(99999, lease)
        assert plate_occupancy.snapshot(99999).lease_unit_id == 7

        plate_occupancy.release_dispatch(99999, "dispatch watchdog")
        assert plate_occupancy.snapshot(99999).lease_unit_id is None
        plate_occupancy.release_dispatch(99999, "dispatch watchdog")  # idempotent
        assert plate_occupancy.snapshot(99999).lease_unit_id is None
