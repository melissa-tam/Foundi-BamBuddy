"""The scheduler stops dispatching into a standing AMS fault, and says why not.

Two halves of the same 001-H2S incident (2026-08-29):

* **W4** — at 01:25:11 a terminal closed incident 59 with ``0700_0006`` still
  standing on the wire; at 01:25:12 the scheduler dispatched item 1010 onto that
  printer, because ``_is_printer_idle`` had never looked at live HMS. For those
  three seconds NO incident row existed, which is why the gate reads the WIRE and
  not the incident store. The print never started and the printer sat out 15 h.
* **W5** — for those 15 h the tick printed ``connected=True, state=IDLE,
  awaiting_plate_clear=False`` every 30 s and named nothing: not the dead claim
  holding the printer, not its age, not the fault. Every fact was in the process.

Plus the boundary that must NOT move: the eject lane is filament-less and stays
dispatchable through a standing AMS fault — otherwise a fault would block the very
sweep that clears the plate.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import print_scheduler as sched_mod
from backend.app.services.bambu_mqtt import HMSError
from backend.app.services.print_scheduler import scheduler


def _ptfe_breakage_hms(ams_id: int = 0, tray_id: int = 3) -> HMSError:
    """``0700_0006`` — the PTFE-tube breakage incident #60 carried. PHYSICAL."""
    attr = 0x07000000 | (ams_id << 16) | ((0x20 + tray_id) << 8)
    return HMSError(code="0x20006", attr=attr, module=7, severity=2, full_code=f"{attr:08X}00020006")


def _informational_hms() -> HMSError:
    """``0700_0025`` — the feed-resistance precursor. Classified INFORMATIONAL by
    the taxonomy precisely so it can never be mistaken for an actionable fault."""
    return HMSError(code="0025", attr=0x07000025, module=7, severity=1, full_code="07000025")


def _state(live: str = "IDLE", hms: list | None = None):
    return SimpleNamespace(state=live, hms_errors=hms or [], subtask_id="task-1")


@pytest.fixture
def healthy_printer(monkeypatch):
    """A printer that passes every OTHER idle gate, so the fault gate is what
    decides."""
    pm = sched_mod.printer_manager
    monkeypatch.setattr(pm, "is_connected", lambda pid: True)
    monkeypatch.setattr(pm, "is_quarantined", lambda pid: False)
    monkeypatch.setattr(pm, "is_model_mismatch", lambda pid: False)
    monkeypatch.setattr(pm, "is_awaiting_plate_clear", lambda pid: False)
    return pm


class TestIsPrinterIdleFaultGate:
    def test_a_standing_actionable_fault_makes_the_printer_not_idle(self, healthy_printer, monkeypatch, caplog):
        """LIVENESS — the exact 01:25:12 hole. No incident row exists here at all;
        the gate reads the wire."""
        monkeypatch.setattr(healthy_printer, "get_status", lambda pid: _state("IDLE", [_ptfe_breakage_hms()]))

        with caplog.at_level("DEBUG", logger="backend.app.services.print_scheduler"):
            assert scheduler._is_printer_idle(4242) is False

        reasons = [r.getMessage() for r in caplog.records]
        assert any("standing AMS fault" in msg and "0700_0006" in msg for msg in reasons)

    def test_an_informational_code_does_not_block_dispatch(self, healthy_printer, monkeypatch):
        """The symmetry guard: the gate uses the SAME classification that decides
        whether an incident would open, so it can only block where the alternative
        is dispatch-then-immediate-fault. An informational code is not that."""
        monkeypatch.setattr(healthy_printer, "get_status", lambda pid: _state("IDLE", [_informational_hms()]))

        assert scheduler._is_printer_idle(4242) is True

    def test_an_empty_hms_list_is_idle(self, healthy_printer, monkeypatch):
        monkeypatch.setattr(healthy_printer, "get_status", lambda pid: _state("IDLE", []))

        assert scheduler._is_printer_idle(4242) is True

    def test_a_fault_free_finish_state_is_still_idle(self, healthy_printer, monkeypatch):
        """The normal farm loop: FINISH with a clean wire dispatches the next unit."""
        monkeypatch.setattr(healthy_printer, "get_status", lambda pid: _state("FINISH", []))

        assert scheduler._is_printer_idle(4242) is True

    def test_the_gate_uses_the_recovery_classifier_not_a_second_code_list(self, monkeypatch):
        """Invariant 1 — one origin. Pinned by substitution: neutralise
        ``live_candidates`` and the gate stops blocking, which is only possible if
        that function is what it consults."""
        pm = sched_mod.printer_manager
        monkeypatch.setattr(pm, "is_connected", lambda pid: True)
        monkeypatch.setattr(pm, "is_quarantined", lambda pid: False)
        monkeypatch.setattr(pm, "is_model_mismatch", lambda pid: False)
        monkeypatch.setattr(pm, "is_awaiting_plate_clear", lambda pid: False)
        monkeypatch.setattr(pm, "get_status", lambda pid: _state("IDLE", [_ptfe_breakage_hms()]))
        assert scheduler._is_printer_idle(4242) is False

        from backend.app.services import spool_recovery

        monkeypatch.setattr(spool_recovery, "live_candidates", lambda _state: frozenset())
        assert scheduler._is_printer_idle(4242) is True


class TestEjectLaneStaysUngated:
    """SCOPE: the fault gate is PRINT dispatch only. An eject is filament-less and
    motion-only — gating it behind an AMS fault would leave the plate occupied and
    the printer stuck behind the very sweep that would free it."""

    def test_the_eject_dispatcher_never_consults_the_scheduler_idle_gate(self):
        """``dispatch_part_present_eject`` is the ONE eject dispatcher (production,
        FA and manual/foreign ejects all funnel through it). Pinned by making the
        scheduler gate explode: if the eject path ever grew a call to it, this
        blows up instead of silently inheriting a dispatch block."""
        import inspect

        from backend.app.services.eject import monitor, remote

        for module in (remote, monitor):
            src = inspect.getsource(module)
            assert "_is_printer_idle" not in src, f"{module.__name__} must not consult the print-dispatch idle gate"

    def test_the_gate_lives_only_where_print_dispatch_decides(self):
        """``_is_printer_idle`` is scheduler-internal and consulted only by the
        pending-item scan, the model-based search and auto-drying. The behavioural
        half of this pin — a real eject dispatching on a printer whose wire carries
        the fault — lives with the dispatcher's own suite
        (``eject/test_remote.py::TestStandingFaultDoesNotGateEjects``), where its
        FTPS/geometry fixtures already exist."""
        import inspect

        from backend.app.services.print_scheduler import PrintScheduler

        callers = [
            stripped
            for stripped in (line.strip() for line in inspect.getsource(PrintScheduler).splitlines())
            if "_is_printer_idle(" in stripped and not stripped.startswith(("#", "def ", "async def "))
        ]
        assert callers, "the gate must still have call sites — this pin is about WHICH"
        assert all("self._is_printer_idle(" in line for line in callers)


class TestBusyDiagnostic:
    """W5: the per-printer "not available" line names the blocker."""

    def test_a_printing_claim_is_named_with_its_id_and_age(self):
        started = datetime.now(timezone.utc) - timedelta(minutes=42)
        cause = sched_mod._busy_cause(7, {7: [(1010, started)]}, set(), _state("IDLE", []))

        assert "printing claim item 1010" in cause
        assert "42 min" in cause

    def test_a_naive_started_at_is_read_as_utc(self):
        """The fork stores ``started_at`` tz-naive; a naive stamp read as local time
        would report a wildly wrong age (the number an operator would act on)."""
        started = (datetime.now(timezone.utc) - timedelta(minutes=15)).replace(tzinfo=None)
        cause = sched_mod._busy_cause(7, {7: [(1010, started)]}, set(), None)

        assert "15 min" in cause

    def test_a_claim_with_no_started_at_says_so_rather_than_guessing(self):
        cause = sched_mod._busy_cause(7, {7: [(1010, None)]}, set(), None)

        assert "age unknown" in cause

    def test_a_post_dispatch_hold_is_named(self):
        assert sched_mod._busy_cause(7, {}, {7}, None) == "post-dispatch hold"

    def test_a_standing_fault_is_named_and_causes_stack(self):
        """A dead claim on a printer with a standing fault is a different story from
        either alone — which is exactly the 001-H2S shape."""
        started = datetime.now(timezone.utc) - timedelta(hours=15)
        cause = sched_mod._busy_cause(7, {7: [(1010, started)]}, set(), _state("IDLE", [_ptfe_breakage_hms()]))

        assert "printing claim item 1010" in cause
        assert "standing fault 0700_0006" in cause

    def test_an_unattributed_busy_printer_says_so(self):
        assert sched_mod._busy_cause(7, {}, set(), _state("IDLE", [])) == "unattributed"

    def test_the_incident_summary_reads_the_projection_cache(self):
        from backend.app.services import printer_incidents

        printer_incidents._reset_state()
        try:
            assert sched_mod._incident_summary(7) == "-"
            printer_incidents._open_cache[7] = {
                "kind": "physical",
                "status": "escalated",
                "slot_desc": "AMS A slot 4",
                "created_at": None,
            }
            assert sched_mod._incident_summary(7) == "physical/escalated@AMS A slot 4"
        finally:
            printer_incidents._reset_state()
