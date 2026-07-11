"""Unit tests for the device-vs-declared model-mismatch gate (Phase 2).

Covers ``PrinterManager.check_model_mismatch`` (the canonical-compare matrix),
the in-memory set/clear helpers, and the scheduler idle-gate block.
"""

from types import SimpleNamespace

import pytest

from backend.app.services.printer_manager import printer_manager


def _wire(monkeypatch, reported, registered):
    monkeypatch.setattr(printer_manager, "get_status", lambda pid: SimpleNamespace(reported_model=reported))
    monkeypatch.setattr(printer_manager, "get_model", lambda pid: registered)


class TestCheckModelMismatch:
    def test_both_null_no_mismatch(self, monkeypatch):
        _wire(monkeypatch, None, None)
        assert printer_manager.check_model_mismatch(1) is None

    def test_reported_null_no_mismatch(self, monkeypatch):
        # The live H2S reports no model field ⇒ reported None ⇒ never a mismatch.
        _wire(monkeypatch, None, "H2S")
        assert printer_manager.check_model_mismatch(1) is None

    def test_registered_null_no_mismatch(self, monkeypatch):
        _wire(monkeypatch, "H2C", None)
        assert printer_manager.check_model_mismatch(1) is None

    def test_equal_canon_no_mismatch(self, monkeypatch):
        # O1S canonicalises to H2S — same printer, not a mismatch.
        _wire(monkeypatch, "O1S", "H2S")
        assert printer_manager.check_model_mismatch(1) is None

    def test_differing_returns_reason(self, monkeypatch):
        _wire(monkeypatch, "H2C", "H2S")
        reason = printer_manager.check_model_mismatch(1)
        assert reason is not None
        assert "H2C" in reason and "H2S" in reason

    def test_no_status_no_mismatch(self, monkeypatch):
        monkeypatch.setattr(printer_manager, "get_status", lambda pid: None)
        monkeypatch.setattr(printer_manager, "get_model", lambda pid: "H2S")
        assert printer_manager.check_model_mismatch(1) is None


class TestModelMismatchState:
    def test_set_and_clear(self):
        pid = 4242
        try:
            assert printer_manager.is_model_mismatch(pid) is False
            printer_manager.set_model_mismatch(pid, "device reports H2C, registered as H2S")
            assert printer_manager.is_model_mismatch(pid) is True
            assert "H2C" in printer_manager.model_mismatch_reason(pid)
            printer_manager.set_model_mismatch(pid, None)
            assert printer_manager.is_model_mismatch(pid) is False
            assert printer_manager.model_mismatch_reason(pid) is None
        finally:
            printer_manager._model_mismatch.pop(pid, None)


class TestSchedulerBlocksOnModelMismatch:
    def test_idle_gate_blocks_when_mismatched(self, monkeypatch):
        from backend.app.services.print_scheduler import scheduler

        monkeypatch.setattr(printer_manager, "is_connected", lambda pid: True)
        monkeypatch.setattr(printer_manager, "is_quarantined", lambda pid: False)
        monkeypatch.setattr(printer_manager, "is_model_mismatch", lambda pid: True)
        # Even a live-IDLE printer must not be considered idle while mismatched.
        monkeypatch.setattr(printer_manager, "get_status", lambda pid: SimpleNamespace(state="IDLE"))
        assert scheduler._is_printer_idle(1) is False

    def test_idle_gate_allows_when_not_mismatched(self, monkeypatch):
        from backend.app.services.print_scheduler import scheduler

        monkeypatch.setattr(printer_manager, "is_connected", lambda pid: True)
        monkeypatch.setattr(printer_manager, "is_quarantined", lambda pid: False)
        monkeypatch.setattr(printer_manager, "is_model_mismatch", lambda pid: False)
        monkeypatch.setattr(printer_manager, "is_awaiting_plate_clear", lambda pid: False)
        monkeypatch.setattr(printer_manager, "get_status", lambda pid: SimpleNamespace(state="IDLE"))
        assert scheduler._is_printer_idle(1) is True
