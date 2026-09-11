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

import pytest

from backend.app.services import print_scheduler as sched_mod, printer_incidents
from backend.app.services.bambu_mqtt import HMSError
from backend.app.services.plate_occupancy import (
    CooldownEject,
    EscalationOnly,
    Evidence,
    PendingEject,
    plate_occupancy,
)
from backend.app.services.print_scheduler import scheduler


@pytest.fixture(autouse=True)
def _clean_authority():
    """The idle gate and the busy diagnostic both read the plate-occupancy
    authority, whose singleton is process-wide — start every case unclaimed."""
    plate_occupancy.reset_for_tests()
    printer_incidents._reset_state()  # the gate reads the incident cache too (2026-09-11)
    yield
    plate_occupancy.reset_for_tests()
    printer_incidents._reset_state()


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


def _evidence(live: str | None = None) -> Evidence:
    """The derived facts a caller hands the authority — never stored by it."""
    return Evidence(live_state=live)


@pytest.fixture
def healthy_printer(monkeypatch):
    """A printer that passes every OTHER idle gate, so the fault gate is what
    decides.

    Nothing stubs the plate here any more: OWNERSHIP is the plate-occupancy
    authority's answer, and the autouse fixture leaves every printer plate-clear and
    unclaimed.
    """
    pm = sched_mod.printer_manager
    monkeypatch.setattr(pm, "is_connected", lambda pid: True)
    monkeypatch.setattr(pm, "is_quarantined", lambda pid: False)
    monkeypatch.setattr(pm, "is_model_mismatch", lambda pid: False)
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
        that function is what it consults.

        Patched on the SCHEDULER's own binding: the taxonomy readers moved into
        ``hms_errors`` and the scheduler imports the name at module level, so
        rebinding it in the defining module would leave this module's reference
        pointing at the original and the pin would silently stop pinning."""
        pm = sched_mod.printer_manager
        monkeypatch.setattr(pm, "is_connected", lambda pid: True)
        monkeypatch.setattr(pm, "is_quarantined", lambda pid: False)
        monkeypatch.setattr(pm, "is_model_mismatch", lambda pid: False)
        monkeypatch.setattr(pm, "get_status", lambda pid: _state("IDLE", [_ptfe_breakage_hms()]))
        assert scheduler._is_printer_idle(4242) is False

        monkeypatch.setattr(sched_mod, "live_candidates", lambda _state: frozenset())
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
    """W5: the per-printer "not available" line names the blocker.

    The ``dispatch_hold_printers`` argument is gone with the scheduler's private
    hold dict (2026-08-30): the occupancy causes — an eject in flight, a dispatch
    lease, a deposit on the plate — are now read from the authority's ``snapshot``,
    which is also why an eject is finally VISIBLE on this line. A printer taking no
    work because a sweep is crossing its plate used to read exactly like one taking
    no work for no reason at all.
    """

    def test_a_printing_claim_is_named_with_its_id_and_age(self):
        started = datetime.now(timezone.utc) - timedelta(minutes=42)
        cause = sched_mod._busy_cause(7, {7: [(1010, started)]}, _state("IDLE", []))

        assert "printing claim item 1010" in cause
        assert "42 min" in cause

    def test_a_naive_started_at_is_read_as_utc(self):
        """The fork stores ``started_at`` tz-naive; a naive stamp read as local time
        would report a wildly wrong age (the number an operator would act on)."""
        started = (datetime.now(timezone.utc) - timedelta(minutes=15)).replace(tzinfo=None)
        cause = sched_mod._busy_cause(7, {7: [(1010, started)]}, None)

        assert "15 min" in cause

    def test_a_claim_with_no_started_at_says_so_rather_than_guessing(self):
        cause = sched_mod._busy_cause(7, {7: [(1010, None)]}, None)

        assert "age unknown" in cause

    def test_a_post_dispatch_hold_is_named(self):
        """The old "post-dispatch hold" line, re-pinned onto the lease that replaced
        it — and it now names the UNIT the printer is being held for, which the
        boolean set could never do."""
        lease = plate_occupancy.claim_for_dispatch(
            7, 1010, pre_state="FINISH", pre_subtask="t-1", min_hold_s=60.0, max_hold_s=180.0, ev=_evidence()
        )
        assert plate_occupancy.commit_dispatch(7, lease) is None

        cause = sched_mod._busy_cause(7, {}, None)
        assert "dispatch lease unit 1010" in cause

    def test_an_eject_in_flight_is_named(self):
        """New surface, same incident family: the eject lane was invisible here."""
        plate_occupancy.hydrate_plate(7, "task-1", EscalationOnly())
        assert (
            plate_occupancy.claim_for_eject(
                7, PendingEject(purpose="production", run_id=3, queue_item_id=1010), _evidence()
            )
            is None
        )

        cause = sched_mod._busy_cause(7, {}, None)
        assert "production eject in flight" in cause
        assert "not started" in cause

    def test_an_occupied_plate_is_named_with_the_policy_holding_it(self):
        plate_occupancy.hydrate_plate(7, "task-1", CooldownEject(unit_id=1010, run_id=3))

        cause = sched_mod._busy_cause(7, {}, None)
        assert "plate occupied (CooldownEject)" in cause

    def test_a_standing_fault_is_named_and_causes_stack(self):
        """A dead claim on a printer with a standing fault is a different story from
        either alone — which is exactly the 001-H2S shape."""
        started = datetime.now(timezone.utc) - timedelta(hours=15)
        cause = sched_mod._busy_cause(7, {7: [(1010, started)]}, _state("IDLE", [_ptfe_breakage_hms()]))

        assert "printing claim item 1010" in cause
        assert "standing fault 0700_0006" in cause

    def test_every_holding_cause_stacks_on_one_line(self):
        """They stack because they are different stories: a dead claim on a printer
        whose plate is also occupied is not either one alone."""
        plate_occupancy.hydrate_plate(7, "task-1", EscalationOnly())
        started = datetime.now(timezone.utc) - timedelta(hours=15)

        cause = sched_mod._busy_cause(7, {7: [(1010, started)]}, _state("IDLE", [_ptfe_breakage_hms()]))

        assert "printing claim item 1010" in cause
        assert "plate occupied (EscalationOnly)" in cause
        assert "standing fault 0700_0006" in cause

    def test_an_unattributed_busy_printer_says_so(self):
        assert sched_mod._busy_cause(7, {}, _state("IDLE", [])) == "unattributed"

    def test_the_incident_summary_reads_the_projection_cache(self):
        from backend.app.services import printer_incidents

        printer_incidents._reset_state()
        try:
            assert sched_mod._incident_summary(7) == "-"
            printer_incidents._open_cache[7] = {
                12: {
                    "id": 12,
                    "kind": "physical",
                    "status": "escalated",
                    "slot_desc": "AMS A slot 4",
                    "created_at": None,
                }
            }
            assert sched_mod._incident_summary(7) == "physical/escalated@AMS A slot 4"
            # A second hold beside it (multi-alarm rule): EVERY open row is named.
            printer_incidents._open_cache[7][13] = {
                "id": 13,
                "kind": "z_reference_lost",
                "status": "escalated",
                "slot_desc": None,
                "created_at": None,
            }
            assert sched_mod._incident_summary(7) == "physical/escalated@AMS A slot 4+z_reference_lost/escalated"
        finally:
            printer_incidents._reset_state()


class TestAmsMidFilamentChangeGate:
    """002-H2S 2026-09-11: the AMS sat at ``ams_status_main == 1`` behind a layer-0
    jam — a state in which the firmware drops every ``ams_change_filament`` — and the
    scheduler dispatched onto it 31 s after the stop. The wedge carries no HMS code of
    its own once the fault clears, so the standing-fault gate above cannot see it.
    """

    def _mid_change(self, live: str = "IDLE"):
        return SimpleNamespace(state=live, hms_errors=[], subtask_id="task-1", ams_status_main=1)

    def test_a_mid_filament_change_ams_makes_the_printer_not_idle(self, healthy_printer, monkeypatch, caplog):
        monkeypatch.setattr(healthy_printer, "get_status", lambda pid: self._mid_change())

        with caplog.at_level("DEBUG", logger="backend.app.services.print_scheduler"):
            assert scheduler._is_printer_idle(4242) is False

        assert any("AMS mid filament-change" in r.getMessage() for r in caplog.records)

    def test_assist_state_three_still_dispatches(self, healthy_printer, monkeypatch):
        """3 is ASSIST — what every RUNNING H2S in the fleet reports. The gate is
        value-1-only by measurement; reading it as "non-idle" would stop the farm."""
        monkeypatch.setattr(
            healthy_printer,
            "get_status",
            lambda pid: SimpleNamespace(state="IDLE", hms_errors=[], subtask_id="t", ams_status_main=3),
        )

        assert scheduler._is_printer_idle(4242) is True

    def test_the_busy_line_names_the_mid_change(self):
        cause = sched_mod._busy_cause(7, {}, self._mid_change())

        assert "AMS mid filament-change" in cause


class TestIdleRefusalCause:
    """ONE owner for "why did the gate say no": ``_is_printer_idle`` writes the token
    on every exit, and the tick reads it. For 15 h on 2026-08-29 the per-printer line
    named nothing; this is the machine-readable half of that fix, and what lets the
    waiting reason and the change log agree without re-deriving anything."""

    def test_a_passing_printer_records_no_refusal(self, healthy_printer, monkeypatch):
        monkeypatch.setattr(healthy_printer, "get_status", lambda pid: _state("IDLE", []))

        assert scheduler._is_printer_idle(4242) is True
        assert scheduler._idle_refusal[4242] is None

    def test_not_connected(self, monkeypatch):
        monkeypatch.setattr(sched_mod.printer_manager, "is_connected", lambda pid: False)

        assert scheduler._is_printer_idle(4242) is False
        assert scheduler._idle_refusal[4242] == "not_connected"

    def test_quarantined(self, healthy_printer, monkeypatch):
        monkeypatch.setattr(healthy_printer, "is_quarantined", lambda pid: True)

        assert scheduler._is_printer_idle(4242) is False
        assert scheduler._idle_refusal[4242] == "quarantined"

    def test_model_mismatch(self, healthy_printer, monkeypatch):
        monkeypatch.setattr(healthy_printer, "is_model_mismatch", lambda pid: True)

        assert scheduler._is_printer_idle(4242) is False
        assert scheduler._idle_refusal[4242] == "model_mismatch"

    def test_no_status(self, healthy_printer, monkeypatch):
        monkeypatch.setattr(healthy_printer, "get_status", lambda pid: None)

        assert scheduler._is_printer_idle(4242) is False
        assert scheduler._idle_refusal[4242] == "no_status"

    def test_standing_fault_names_its_codes_sorted(self, healthy_printer, monkeypatch):
        monkeypatch.setattr(
            healthy_printer,
            "get_status",
            lambda pid: _state("IDLE", [_ptfe_breakage_hms(tray_id=3), _ptfe_breakage_hms(tray_id=1)]),
        )

        assert scheduler._is_printer_idle(4242) is False
        assert scheduler._idle_refusal[4242] == "standing_fault:0700_0006"

    def test_ams_filament_change(self, healthy_printer, monkeypatch):
        monkeypatch.setattr(
            healthy_printer,
            "get_status",
            lambda pid: SimpleNamespace(state="IDLE", hms_errors=[], subtask_id="t", ams_status_main=1),
        )

        assert scheduler._is_printer_idle(4242) is False
        assert scheduler._idle_refusal[4242] == "ams_filament_change"

    def test_occupancy_carries_the_authoritys_own_refusal_literal(self, healthy_printer, monkeypatch):
        """The token is the ``TransitionRefusal`` the authority returned, verbatim —
        re-spelling it here would give the same fact two vocabularies."""
        monkeypatch.setattr(healthy_printer, "get_status", lambda pid: _state("IDLE", []))
        plate_occupancy.hydrate_plate(4242, "task-1", EscalationOnly())

        assert scheduler._is_printer_idle(4242) is False
        token = scheduler._idle_refusal[4242]
        assert token.startswith("occupancy:")
        assert token.split(":", 1)[1]

    def test_a_non_idle_state_names_the_state(self, healthy_printer, monkeypatch):
        monkeypatch.setattr(healthy_printer, "get_status", lambda pid: _state("OFFLINE", []))

        assert scheduler._is_printer_idle(4242) is False
        assert scheduler._idle_refusal[4242] == "state:OFFLINE"


class TestRefusalChangeLog:
    """The 001-H2S line printed 1,800 times over 15 h and said nothing new each
    time. Visibility on CHANGE: an unchanged cause is silent, so the lines that DO
    appear are the transitions."""

    @pytest.fixture(autouse=True)
    def _clean_refusal_state(self):
        scheduler._idle_refusal.clear()
        scheduler._last_logged_refusal.clear()
        yield
        scheduler._idle_refusal.clear()
        scheduler._last_logged_refusal.clear()

    def _lines(self, caplog):
        return [r.getMessage() for r in caplog.records if "dispatch refusal changed" in r.getMessage()]

    def test_an_unchanged_refusal_logs_once_not_every_tick(self, caplog):
        scheduler._idle_refusal[7] = "ams_filament_change"
        with caplog.at_level("INFO", logger="backend.app.services.print_scheduler"):
            scheduler._log_refusal_changes()
            scheduler._log_refusal_changes()

        assert len(self._lines(caplog)) == 1

    def test_the_first_line_names_the_cause_and_the_previous_none(self, caplog):
        scheduler._idle_refusal[7] = "ams_filament_change"
        with caplog.at_level("INFO", logger="backend.app.services.print_scheduler"):
            scheduler._log_refusal_changes()

        line = self._lines(caplog)[0]
        assert "cause=ams_filament_change" in line
        assert "was none" in line

    def test_becoming_idle_logs_cause_none(self, caplog):
        scheduler._idle_refusal[7] = "ams_filament_change"
        with caplog.at_level("INFO", logger="backend.app.services.print_scheduler"):
            scheduler._log_refusal_changes()
            scheduler._idle_refusal[7] = None
            scheduler._log_refusal_changes()

        assert "cause=none" in self._lines(caplog)[1]

    def test_a_printer_that_was_never_refused_and_is_idle_is_silent(self, caplog):
        scheduler._idle_refusal[7] = None
        with caplog.at_level("INFO", logger="backend.app.services.print_scheduler"):
            scheduler._log_refusal_changes()

        assert self._lines(caplog) == []

    def test_the_tick_calls_it(self):
        """The log is only worth anything if the tick actually runs it."""
        import inspect

        assert "_log_refusal_changes()" in inspect.getsource(sched_mod.PrintScheduler.check_queue)


class TestTheRecordGate:
    """WS7 (2026-09-11, 003-H2S): dispatch reads the EQUIPMENT RECORD beside the wire.
    The firmware wipes its HMS list at every terminal, so the wire read clean while
    filament was still stuck in the shared PTFE path — and the next unit dispatched
    into it. Three times."""

    @staticmethod
    def _hold(printer_id: int, kind: str = "physical") -> None:
        printer_incidents._open_cache[printer_id] = {
            5: {"id": 5, "kind": kind, "status": "escalated", "slot_desc": None, "created_at": None}
        }

    def test_an_open_incident_refuses_dispatch_with_its_kinds(self, healthy_printer, monkeypatch):
        monkeypatch.setattr(healthy_printer, "get_status", lambda pid: _state("IDLE", []))
        self._hold(4242)

        assert scheduler._is_printer_idle(4242) is False
        assert scheduler._idle_refusal[4242] == "incident:physical"
        assert "open incident(s) physical" in sched_mod._busy_cause(4242, {}, _state("IDLE", []))

    def test_a_closed_record_admits_again(self, healthy_printer, monkeypatch):
        monkeypatch.setattr(healthy_printer, "get_status", lambda pid: _state("IDLE", []))
        self._hold(4242)
        assert scheduler._is_printer_idle(4242) is False

        printer_incidents._reset_state()
        assert scheduler._is_printer_idle(4242) is True
        assert scheduler._idle_refusal[4242] is None

    def test_the_eject_lane_ignores_the_record(self):
        """A physical hold and an occupied plate: the sweep still dispatches (08-29
        gotcha d) — an eject is filament-less, and gating it behind a filament fault
        would deadlock the plate that holds the printer."""
        self._hold(4242)
        plate_occupancy.hydrate_plate(4242, "task-1", EscalationOnly())

        assert plate_occupancy.ejectable(4242, _evidence("IDLE")) is None
