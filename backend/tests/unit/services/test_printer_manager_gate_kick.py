"""Plate-gate release fires a dispatch kick — release-edge-guarded (latency Phase A).

**Moved, not retired (2026-08-30 occupancy cut-over).** These behaviours used to hang
off ``printer_manager.set_awaiting_plate_clear``, which was the single choke point for
every gate release and owned the kick itself. That method is gone; the kick is now one
of the occupancy authority's four injected side effects — ``plate_occupancy_store.
kick_scheduler``, which publishes the unchanged ``dispatch_kick.kick(
"plate_gate_release", printer_id)`` — and the core fires it on RELEASE EDGES ONLY.

So the tests below wire the REAL store callable into the authority and drive REAL
transitions, which keeps the three original facts pinned end to end: a genuine release
kicks the scheduler with the established reason, raising the gate does not, and a
redundant clear does not. Two edges the old boolean flag could not even express are
pinned beside them:

* a dropped EJECT is also a release edge (the printer becomes dispatchable when the
  sweep's claim goes away, whatever the plate is doing);
* a ``hydrate`` never kicks — waking the scheduler mid-startup, before the reconcilers
  have decided what a rebuilt record means, is the one thing startup must not do.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.app.services import plate_occupancy_store
from backend.app.services.plate_occupancy import (
    DepositEvidence,
    EscalationOnly,
    PendingEject,
    TerminalDisposition,
    plate_occupancy,
)

PRINTER = 99

# The one reason string the scheduler's wake-reason ring knows for this edge. The
# core's *cause* names the transition and is logged, not published, so every release
# edge must still arrive under this single spelling.
KICK_REASON = "plate_gate_release"


@pytest.fixture(autouse=True)
def _clean_authority():
    """Isolate the module-singleton authority and wire ONLY the kick lane.

    ``reset_for_tests`` un-wires every callable, so persist and broadcast stay at
    their no-op defaults here — no loop, no DB, no websocket needed for a kick
    assertion (what the old fixture achieved by patching ``_schedule_async``).
    """
    plate_occupancy.reset_for_tests()
    plate_occupancy.configure(kick=plate_occupancy_store.kick_scheduler)
    yield
    plate_occupancy.reset_for_tests()


def _deposit() -> DepositEvidence:
    """A terminal that unambiguously left a part on the plate."""
    return DepositEvidence(
        final_status="completed",
        is_dry_run=False,
        peaks_reliable=True,
        last_layer_num=120,
        last_progress=100.0,
    )


def _terminal() -> TerminalDisposition:
    """A deposit-bearing terminal the raise guard allows through."""
    return TerminalDisposition(
        queue_item_id=None,
        source_subtask_id="SUB-1",
        evidence=_deposit(),
        policy=EscalationOnly(),
        raise_gate=True,
    )


def _pending() -> PendingEject:
    return PendingEject(purpose="production", run_id=1, queue_item_id=5)


def test_plate_release_kicks_the_scheduler():
    """OCCUPIED → CLEAR is a release edge: the freed printer must dispatch in ~1 s."""
    plate_occupancy.note_terminal(PRINTER, _terminal())
    assert plate_occupancy.is_plate_occupied(PRINTER) is True

    with patch("backend.app.services.dispatch_kick.dispatch_kick") as mock_dk:
        assert plate_occupancy.clear_plate(PRINTER) is None

    mock_dk.kick.assert_called_once_with(KICK_REASON, PRINTER)


def test_raising_the_gate_does_not_kick():
    """A raise makes the printer LESS dispatchable — kicking would wake the scheduler
    into a printer it must not touch."""
    with patch("backend.app.services.dispatch_kick.dispatch_kick") as mock_dk:
        plate_occupancy.note_terminal(PRINTER, _terminal())

    assert plate_occupancy.is_plate_occupied(PRINTER) is True
    mock_dk.kick.assert_not_called()


def test_redundant_clear_does_not_kick():
    """Clearing an already-clear plate is refused, not a transition — so no fan-out."""
    with patch("backend.app.services.dispatch_kick.dispatch_kick") as mock_dk:
        assert plate_occupancy.clear_plate(PRINTER) == "not_occupied"

    mock_dk.kick.assert_not_called()


def test_a_dropped_eject_kicks_even_while_the_plate_stays_occupied():
    """The second release edge, which the old boolean flag could not express.

    ``drop_hydrated_eject`` retires the startup reconciler's unverifiable claim and
    deliberately leaves the plate alone. The plate still blocks a unit dispatch, but
    the eject slot no longer does — and the kick is what lets the eject lane re-claim
    the printer promptly instead of waiting out a poll interval.
    """
    plate_occupancy.hydrate_plate(PRINTER, "SUB-1", EscalationOnly())
    plate_occupancy.hydrate_eject(PRINTER, _pending())

    with patch("backend.app.services.dispatch_kick.dispatch_kick") as mock_dk:
        assert plate_occupancy.drop_hydrated_eject(PRINTER, "reconciled") is True

    mock_dk.kick.assert_called_once_with(KICK_REASON, PRINTER)
    assert plate_occupancy.is_plate_occupied(PRINTER) is True


def test_hydration_never_kicks():
    """``cause="hydrate"`` skips persist, broadcast AND kick.

    The state was just READ from the DB, and a kick would wake the scheduler
    mid-startup — before ``reconcile_pending_ejects_on_startup`` has decided what the
    rebuilt records mean. Both hydration entry points are pinned, including the eject
    one, whose ``before`` view is empty and would otherwise look like an edge.
    """
    with patch("backend.app.services.dispatch_kick.dispatch_kick") as mock_dk:
        plate_occupancy.hydrate_plate(PRINTER, "SUB-1", EscalationOnly())
        plate_occupancy.hydrate_eject(PRINTER, _pending())

    mock_dk.kick.assert_not_called()


def test_a_failing_scheduler_never_unwinds_the_release():
    """The kick is a latency optimisation; the release already happened.

    ``kick_scheduler`` swallows its own failure and the core swallows anything it
    misses, so a scheduler that raises must not leave the plate gated — that would
    trade a slow dispatch for a stranded printer.
    """
    plate_occupancy.note_terminal(PRINTER, _terminal())

    with patch("backend.app.services.dispatch_kick.dispatch_kick") as mock_dk:
        mock_dk.kick.side_effect = RuntimeError("scheduler down")
        assert plate_occupancy.clear_plate(PRINTER) is None

    assert plate_occupancy.is_plate_occupied(PRINTER) is False
