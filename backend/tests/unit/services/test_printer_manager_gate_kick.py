"""Plate-gate release fires a dispatch kick — transition-guarded (latency Phase A).

``set_awaiting_plate_clear(pid, False)`` is the single choke point for every gate
release (manual clear, eject-verified completion, FA approve, startup hygiene). A
real True→False transition must kick the scheduler; raising the gate (True) or a
redundant clear (already False) must NOT.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.app.services.printer_manager import printer_manager


@pytest.fixture(autouse=True)
def _clean_gate_state():
    # Isolate the module-singleton gate set + suppress the loop-scheduled persist/
    # broadcast (no running loop needed for the kick assertion).
    printer_manager._awaiting_plate_clear.discard(99)
    with patch.object(printer_manager, "_schedule_async", MagicMock()):
        yield
    printer_manager._awaiting_plate_clear.discard(99)


def test_true_to_false_transition_kicks():
    printer_manager._awaiting_plate_clear.add(99)  # gate currently raised
    with patch("backend.app.services.dispatch_kick.dispatch_kick") as mock_dk:
        printer_manager.set_awaiting_plate_clear(99, False)
    mock_dk.kick.assert_called_once_with("plate_gate_release", 99)


def test_raising_gate_does_not_kick():
    with patch("backend.app.services.dispatch_kick.dispatch_kick") as mock_dk:
        printer_manager.set_awaiting_plate_clear(99, True)
    mock_dk.kick.assert_not_called()


def test_redundant_clear_does_not_kick():
    # Gate already down (99 not in the set) → clearing again is a no-op transition.
    with patch("backend.app.services.dispatch_kick.dispatch_kick") as mock_dk:
        printer_manager.set_awaiting_plate_clear(99, False)
    mock_dk.kick.assert_not_called()
