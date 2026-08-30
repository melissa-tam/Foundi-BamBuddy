"""Scheduler must exclude quarantined printers from dispatch (Phase 3)."""

from types import SimpleNamespace

import pytest

from backend.app.services.plate_occupancy import plate_occupancy


@pytest.fixture(autouse=True)
def _clean_authority():
    """``_is_printer_idle`` asks the plate-occupancy authority for OWNERSHIP, so the
    quarantine gate is only decisive against an unclaimed, plate-clear printer."""
    plate_occupancy.reset_for_tests()
    yield
    plate_occupancy.reset_for_tests()


class TestSchedulerQuarantineExclusion:
    def test_quarantined_printer_is_not_idle(self, monkeypatch):
        from backend.app.services import print_scheduler as sched_mod
        from backend.app.services.print_scheduler import scheduler

        pm = sched_mod.printer_manager
        monkeypatch.setattr(pm, "is_connected", lambda pid: True)
        monkeypatch.setattr(pm, "get_status", lambda pid: SimpleNamespace(state="IDLE"))

        # Baseline: a connected, idle, non-quarantined printer IS idle.
        monkeypatch.setattr(pm, "is_quarantined", lambda pid: False)
        assert scheduler._is_printer_idle(4242) is True

        # Quarantined → excluded from dispatch (not idle), even while IDLE.
        monkeypatch.setattr(pm, "is_quarantined", lambda pid: True)
        assert scheduler._is_printer_idle(4242) is False

    def test_is_quarantined_reflects_set_state(self):
        from backend.app.services.printer_manager import printer_manager

        assert printer_manager.is_quarantined(999123) is False
        printer_manager.set_quarantined(999123, True)
        try:
            assert printer_manager.is_quarantined(999123) is True
        finally:
            printer_manager.set_quarantined(999123, False)
        assert printer_manager.is_quarantined(999123) is False
