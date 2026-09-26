"""The equipment-fault rule table — every cell, and the evidence each one turns on.

``incident_resolution`` is the ONE answer to "does this occasion end this row". Before
it, six closers each carried their own ``if resolution == …`` chain and the repair
class's motion evidence lived in ``spool_recovery`` module globals — so adding an
evidence meant editing several closers, and 011-H2S 2026-09-17 sat escalated for a day
with a clean wire, an idle printer and a completed print through the repaired path,
because the commonest fleet repair was in nobody's vocabulary.

Three properties are pinned here and nowhere else:

* **the table is TOTAL** — every ``(class, occasion)`` pair is registered, and an
  unregistered class RAISES rather than inheriting the wire's evidence from an
  ``else`` (the ``declared``-class lesson, made structural);
* **the completed arm is QUALIFIED** — five separate ways the "the job completed"
  reading could be wrong, each pinned by its own case;
* **the ledger records EDGES and POSITIVE readings**, never levels, and a session
  boundary can fabricate neither.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models.printer_incident import (
    KIND_JAM,
    KIND_PHYSICAL,
    KIND_PLATE_VISION,
    KIND_SERVICE_HOLD,
    KIND_Z_REFERENCE_LOST,
    RESOLUTION_DECLARED,
    RESOLUTION_JOB_PAUSE,
    RESOLUTION_OPERATOR,
    RESOLUTION_REPAIR,
    RESOLUTION_WIRE,
    RESOLVE_JOB_ENDED_UNSEEN,
    RESOLVE_OBSERVED_RUNNING,
    RESOLVE_OPERATOR,
    RESOLVE_REARM,
    RESOLVE_REPAIR_COMPLETED,
    RESOLVE_REPAIR_OBSERVED,
    RESOLVE_TERMINAL,
    RESOLVE_WIRE_CLEAR,
    RESOLVES_ON,
    STATUS_ESCALATED,
    STATUS_RECOVERING,
    PrinterIncident,
)
from backend.app.services import incident_resolution, printer_incidents
from backend.app.services.bambu_mqtt import HMSError, PrinterState
from backend.app.services.hms_errors import candidate_fingerprint, live_candidates
from backend.app.services.incident_resolution import (
    ClearedEvent,
    Context,
    MotionLedger,
    TerminalEvent,
    driver_owns,
    path_quiet,
    resolve,
)
from backend.app.services.plate_occupancy import EscalationOnly, PendingEject, plate_occupancy

_OCCASIONS = ("running_edge", "job_terminal", "sweep_tick", "startup", "plate_cleared", "new_fault")

# One representative kind per resolution class. The table is keyed by CLASS, so the kind
# is only how a row acquires one — and reading the class off the store (rather than
# spelling it on the row) is exactly what the closers do.
_KIND_BY_CLASS = {
    RESOLUTION_WIRE: (KIND_JAM, "0700_8006"),
    RESOLUTION_REPAIR: (KIND_PHYSICAL, "0700_8004"),
    RESOLUTION_OPERATOR: (KIND_Z_REFERENCE_LOST, ""),
    RESOLUTION_JOB_PAUSE: (KIND_PLATE_VISION, "0500_808C"),
    RESOLUTION_DECLARED: (KIND_SERVICE_HOLD, ""),
}

_JOB = "job-1"
_OPENED_AT = datetime(2026, 9, 17, 9, 43, 0)


@pytest.fixture(autouse=True)
def _clean_authority():
    """The eject exclusion asks the plate authority, which is process state."""
    plate_occupancy.reset_for_tests()
    yield
    plate_occupancy.reset_for_tests()


def _row(resolution: str, *, status: str = STATUS_ESCALATED, printer_id: int = 7) -> PrinterIncident:
    """An open row of the given resolution class. Unattached — ``resolve`` is DB-free."""
    kind, code = _KIND_BY_CLASS[resolution]
    return PrinterIncident(
        id=1,
        printer_id=printer_id,
        job_id=_JOB,
        item_id=None,
        kind=kind,
        code=code,
        codes=f"{kind}:{code}",
        slot_global_tray=None,
        status=status,
        created_at=_OPENED_AT,
    )


def _ptfe_breakage_hms(ams_id: int = 0, tray_id: int = 3) -> HMSError:
    """``0700_0006`` — an ACTIONABLE physical fault, i.e. the path is not quiet."""
    attr = 0x07000000 | (ams_id << 16) | ((0x20 + tray_id) << 8)
    return HMSError(code="0x20006", attr=attr, module=7, severity=2, full_code=f"{attr:08X}00020006")


def _state(live: str = "IDLE", *, hms=None, tray_now=None, epoch=1, ams_status_main=0) -> PrinterState:
    st = PrinterState()
    st.state = live
    st.subtask_id = _JOB
    st.hms_errors = hms or []
    st.tray_now = tray_now
    st.connection_epoch = epoch
    st.ams_status_main = ams_status_main
    return st


def _ledger_with(*, load_at=None, ran_at=None, printer_id: int = 7) -> MotionLedger:
    """A ledger pre-loaded with the motion this case means to have happened.

    ``ran_at`` is the QUALIFIED "a print ran through the path" sighting. The cases that
    are ABOUT what qualifies a sighting drive ``MotionLedger.observe`` instead
    (``TestTheSightingIsQualified``, ``TestTheNewFaultOccasion``)."""
    ledger = MotionLedger()
    if load_at is not None:
        ledger._load_completed_at[printer_id] = load_at  # noqa: SLF001
    if ran_at is not None:
        ledger._path_ran_at[printer_id] = ran_at  # noqa: SLF001
    return ledger


def _jam_hms() -> HMSError:
    """``0700_8010`` — an ACTIONABLE mechanical feed fault (the jam 002-H2S kept raising)."""
    return HMSError(code="8010", attr=0x07000000, module=7, severity=2)


def _permissive(occasion: str) -> Context:
    """The context under which a cell closes IF its class closes on that occasion.

    Deliberately maximal: every piece of evidence any cell could want is present, so a
    "stand" in the expectations below is the CLASS refusing the occasion, never this
    fixture failing to supply something. The printer is RUNNING the row's own job with
    no eject owning it — the one reading every class that closes on the wire closes on
    (a positive non-PAUSE state for ``wire``, a print through the path for ``repair``,
    the paused job printing again for ``job_pause``). For ``new_fault`` the wire also
    carries a DIFFERENT actionable fault than the row's own (its occasion's meaning).
    """
    after = _OPENED_AT + timedelta(seconds=30)
    return Context(
        state=_state("RUNNING", hms=[_jam_hms()] if occasion == "new_fault" else None),
        ledger=_ledger_with(load_at=after, ran_at=after),
        driver_live=False,
        terminal=TerminalEvent(status="completed", eject=False, job_id=_JOB),
        cleared=ClearedEvent(recover=True),
    )


# (class, occasion) -> the source it closes with, or None for "stand".
_EXPECTED: dict[tuple[str, str], str | None] = {
    (RESOLUTION_WIRE, "running_edge"): RESOLVE_OBSERVED_RUNNING,
    (RESOLUTION_WIRE, "job_terminal"): RESOLVE_TERMINAL,
    (RESOLUTION_WIRE, "sweep_tick"): RESOLVE_WIRE_CLEAR,
    (RESOLUTION_WIRE, "startup"): RESOLVE_REARM,
    (RESOLUTION_WIRE, "plate_cleared"): None,
    # Open while a new fault arrives = the fault arrived INSIDE the hold (the wire row
    # closes on its own resume); the outrank test re-classifies, nothing here closes.
    (RESOLUTION_WIRE, "new_fault"): None,
    (RESOLUTION_REPAIR, "running_edge"): None,
    (RESOLUTION_REPAIR, "job_terminal"): RESOLVE_REPAIR_COMPLETED,
    (RESOLUTION_REPAIR, "sweep_tick"): RESOLVE_REPAIR_OBSERVED,
    (RESOLUTION_REPAIR, "startup"): RESOLVE_REARM,
    (RESOLUTION_REPAIR, "plate_cleared"): RESOLVE_OPERATOR,
    (RESOLUTION_REPAIR, "new_fault"): RESOLVE_REPAIR_OBSERVED,
    (RESOLUTION_OPERATOR, "running_edge"): None,
    (RESOLUTION_OPERATOR, "job_terminal"): None,
    (RESOLUTION_OPERATOR, "sweep_tick"): None,
    (RESOLUTION_OPERATOR, "startup"): None,
    (RESOLUTION_OPERATOR, "plate_cleared"): RESOLVE_OPERATOR,
    (RESOLUTION_OPERATOR, "new_fault"): None,
    (RESOLUTION_JOB_PAUSE, "running_edge"): RESOLVE_OBSERVED_RUNNING,
    (RESOLUTION_JOB_PAUSE, "job_terminal"): RESOLVE_TERMINAL,
    (RESOLUTION_JOB_PAUSE, "sweep_tick"): RESOLVE_OBSERVED_RUNNING,
    (RESOLUTION_JOB_PAUSE, "startup"): RESOLVE_REARM,
    (RESOLUTION_JOB_PAUSE, "plate_cleared"): None,
    (RESOLUTION_JOB_PAUSE, "new_fault"): None,
    (RESOLUTION_DECLARED, "running_edge"): None,
    (RESOLUTION_DECLARED, "job_terminal"): None,
    (RESOLUTION_DECLARED, "sweep_tick"): None,
    (RESOLUTION_DECLARED, "startup"): None,
    (RESOLUTION_DECLARED, "plate_cleared"): None,
    (RESOLUTION_DECLARED, "new_fault"): None,
}


class TestTheTableIsTotal:
    def test_every_class_times_every_occasion_is_registered(self):
        """A missing cell is a hold nobody wrote a rule for. The registry is the pin:
        a fifth resolution class must arrive with five rows, not inherit four."""
        expected = {(cls, occ) for cls in _KIND_BY_CLASS for occ in _OCCASIONS}
        assert set(incident_resolution._TABLE) == expected  # noqa: SLF001

    def test_an_unregistered_class_raises_instead_of_falling_through(self, monkeypatch):
        """THE ``declared`` lesson. Three closers once treated "not operator / not
        repair" as the WIRE lane through a bare ``else``, so a hold no evidence closes
        would have been closed by a screen-started print, a clean wire, or the first
        restart. A loud KeyError is the only safe default."""
        monkeypatch.setattr(incident_resolution.printer_incidents, "resolution_class", lambda *a, **k: "invented")

        with pytest.raises(KeyError):
            resolve(_row(RESOLUTION_WIRE), "sweep_tick", _permissive("sweep_tick"))


class TestEveryCell:
    @pytest.mark.parametrize(("resolution", "occasion"), sorted(_EXPECTED))
    def test_cell(self, resolution, occasion):
        """The full class x occasion matrix under maximal evidence."""
        verdict = resolve(_row(resolution), occasion, _permissive(occasion))
        expected_source = _EXPECTED[(resolution, occasion)]

        assert verdict.close is (expected_source is not None), verdict.evidence
        assert verdict.source == expected_source
        # Every verdict carries a sentence in BOTH directions — the closers log it, so
        # a silent stand is a hold nobody can explain from the log.
        assert verdict.evidence

    @pytest.mark.parametrize("resolution", sorted(_KIND_BY_CLASS))
    def test_only_the_sweep_ever_asks_for_a_dwell(self, resolution):
        """The dwell belongs to the per-tick sweep alone: a restart IS the fresh
        derivation it substitutes for, and the three told-occasions are events."""
        for occasion in _OCCASIONS:
            verdict = resolve(_row(resolution), occasion, _permissive(occasion))
            assert verdict.dwell is (occasion == "sweep_tick" and verdict.close), (resolution, occasion)


class TestRecoverAttributeMatchesTheTable:
    """``closed_by_recover`` is a READ of the model's per-class ``RECOVER_ENDS``, never a
    second hand-written reading of the rule table — and this pins the two to one answer.

    The store may not import the rule table, so the attribute and the table's own
    ``plate_cleared``-with-Recover verdict could drift silently; this walks EVERY
    registered ``(kind, external)`` and asks both.
    """

    # A short code per (kind, external) whose taxonomy verdict puts the row on that
    # hardware — asserted below, so a wrong pick fails loudly instead of testing the
    # wrong cell.
    _CODES = {
        (KIND_JAM, False): "0700_8006",
        (KIND_JAM, True): "07FF_8006",
        ("runout", False): "0700_8011",
        ("runout", True): "07FF_8011",
        (KIND_PHYSICAL, False): "0700_8004",
        (KIND_PHYSICAL, True): "07FF_8003",
        ("power_loss", False): "0300_8007",
        (KIND_PLATE_VISION, False): "0500_808C",
        (KIND_Z_REFERENCE_LOST, False): "",
        (KIND_SERVICE_HOLD, False): "",
    }

    def test_every_registered_row_is_covered(self):
        assert set(self._CODES) == set(RESOLVES_ON)

    @pytest.mark.parametrize(("kind", "external"), sorted(RESOLVES_ON))
    def test_recover_ends_it_exactly_when_the_table_closes_it_on_recover(self, kind, external):
        row = PrinterIncident(
            id=1,
            printer_id=7,
            job_id=_JOB,
            item_id=None,
            kind=kind,
            code=self._CODES[(kind, external)],
            codes="",
            slot_global_tray=None,
            status=STATUS_ESCALATED,
            created_at=_OPENED_AT,
        )
        assert printer_incidents.row_external(row) is external, "the fixture code names the wrong hardware"

        verdict = resolve(row, "plate_cleared", _permissive("plate_cleared"))

        assert verdict.close is printer_incidents.closed_by_recover(kind, external=external)


class TestTheJobPauseLane:
    """The printer paused ONE job to ask a human (its own plate check). The answer is that
    job — resumed or stopped — and every cell is bound to the row's OWN job."""

    def _ctx(self, *, live="RUNNING", job=_JOB, driver_live=False, terminal=None) -> Context:
        state = _state(live)
        state.subtask_id = job
        return Context(state=state, ledger=MotionLedger(), driver_live=driver_live, terminal=terminal)

    def test_the_paused_job_running_again_closes_it(self):
        verdict = resolve(_row(RESOLUTION_JOB_PAUSE), "running_edge", self._ctx())
        assert verdict.close is True
        assert verdict.source == RESOLVE_OBSERVED_RUNNING

    def test_another_jobs_running_edge_stands(self):
        verdict = resolve(_row(RESOLUTION_JOB_PAUSE), "running_edge", self._ctx(job="other-job"))
        assert verdict.close is False

    def test_a_live_driver_defers_the_running_edge(self):
        """The wire cell's rule: a RUNNING sample during a resume a driver published is
        its reading, not the hold's answer."""
        verdict = resolve(_row(RESOLUTION_JOB_PAUSE), "running_edge", self._ctx(driver_live=True))
        assert verdict.close is False
        assert "driver" in verdict.evidence

    @pytest.mark.parametrize("status", ["completed", "failed", "aborted", "cancelled"])
    def test_the_paused_jobs_terminal_closes_it_whatever_the_status(self, status):
        """Resumed-and-finished or stopped: either way the job it asked about is over.
        What happens to the plate is the terminal verdict's, captured before this ran."""
        terminal = TerminalEvent(status=status, eject=False, job_id=_JOB)
        verdict = resolve(_row(RESOLUTION_JOB_PAUSE), "job_terminal", self._ctx(live="FAILED", terminal=terminal))
        assert verdict.close is True
        assert verdict.source == RESOLVE_TERMINAL

    def test_another_jobs_terminal_stands(self):
        terminal = TerminalEvent(status="failed", eject=False, job_id="other-job")
        assert resolve(_row(RESOLUTION_JOB_PAUSE), "job_terminal", self._ctx(terminal=terminal)).close is False

    def test_an_eject_terminal_stands(self):
        terminal = TerminalEvent(status="completed", eject=True, job_id=_JOB)
        assert resolve(_row(RESOLUTION_JOB_PAUSE), "job_terminal", self._ctx(terminal=terminal)).close is False

    @pytest.mark.parametrize("occasion", ["sweep_tick", "startup"])
    def test_a_pause_of_the_same_job_is_this_holds_normal_reading(self, occasion):
        assert resolve(_row(RESOLUTION_JOB_PAUSE), occasion, self._ctx(live="PAUSE")).close is False

    @pytest.mark.parametrize("live", ["", "UNKNOWN"])
    def test_a_printer_that_says_nothing_is_not_evidence(self, live):
        assert resolve(_row(RESOLUTION_JOB_PAUSE), "sweep_tick", self._ctx(live=live)).close is False
        assert resolve(_row(RESOLUTION_JOB_PAUSE), "sweep_tick", self._ctx(live=live, job="other-job")).close is False

    def test_a_disconnected_printer_is_not_evidence(self):
        ctx = Context(state=None, ledger=MotionLedger(), driver_live=False)
        assert resolve(_row(RESOLUTION_JOB_PAUSE), "sweep_tick", ctx).close is False

    @pytest.mark.parametrize("occasion", ["sweep_tick", "startup"])
    def test_an_eject_owning_the_printer_is_not_the_answer(self, occasion):
        """The shared "RUNNING and no eject owns it" reading — a sweep is not the paused job."""
        plate_occupancy.hydrate_eject(7, PendingEject(purpose="production", run_id=None, queue_item_id=1))
        assert resolve(_row(RESOLUTION_JOB_PAUSE), occasion, self._ctx()).close is False

    def test_the_sweep_waits_out_its_dwell_and_the_restart_does_not(self):
        sweep = resolve(_row(RESOLUTION_JOB_PAUSE), "sweep_tick", self._ctx())
        startup = resolve(_row(RESOLUTION_JOB_PAUSE), "startup", self._ctx())
        assert (sweep.close, sweep.dwell) == (True, True)
        assert (startup.close, startup.dwell, startup.source) == (True, False, RESOLVE_REARM)

    @pytest.mark.parametrize("recover", [True, False])
    def test_neither_plate_verb_answers_a_paused_job(self, recover):
        """ "The plate is clear" is not "resume the print"."""
        ctx = Context(state=_state("PAUSE"), ledger=MotionLedger(), driver_live=False, cleared=ClearedEvent(recover))
        assert resolve(_row(RESOLUTION_JOB_PAUSE), "plate_cleared", ctx).close is False


class TestTheJobPauseIdentityCells:
    """The job-pause cells ask ``job_identity.same_job`` and read ``unknown`` the way this table
    always read a missing id: an echo naming no job matches only a row that recorded none, and an
    id on ONE side is a different job. ``""`` and ``"0"`` are both "names no job"."""

    @staticmethod
    def _resolve(occasion: str, *, row_job: str, live: str, echo: str):
        row = _row(RESOLUTION_JOB_PAUSE)
        row.job_id = row_job
        state = _state(live)
        state.subtask_id = echo
        return resolve(row, occasion, Context(state=state, ledger=MotionLedger(), driver_live=False))

    @pytest.mark.parametrize(
        ("row_job", "echo", "closes"),
        [
            (_JOB, _JOB, True),
            (_JOB, f" {_JOB} ", True),
            (_JOB, "other-job", False),
            (_JOB, "", False),
            (_JOB, "0", False),
            ("", "", True),
            ("", "0", True),
            ("0", "0", True),
            ("", _JOB, False),
        ],
        ids=[
            "same",
            "same-padded",
            "other",
            "echo-none",
            "echo-zero",
            "both-none",
            "none-zero",
            "both-zero",
            "row-none",
        ],
    )
    def test_the_running_edge(self, row_job, echo, closes):
        assert self._resolve("running_edge", row_job=row_job, live="RUNNING", echo=echo).close is closes

    @pytest.mark.parametrize(
        ("row_job", "echo", "ended"),
        [
            (_JOB, "other-job", True),
            (_JOB, "0", True),
            ("", _JOB, True),
            (_JOB, "", False),
            ("", "0", False),
        ],
        ids=["other", "lan-print", "row-none-echo-named", "silent-echo", "both-name-none"],
    )
    def test_another_job_on_the_printer(self, row_job, echo, ended):
        """``PAUSE`` keeps the RUNNING close out of the way: only the "another job" reading answers."""
        verdict = self._resolve("sweep_tick", row_job=row_job, live="PAUSE", echo=echo)
        assert (verdict.close and verdict.source == RESOLVE_JOB_ENDED_UNSEEN) is ended


class TestAJobPauseCannotOutliveItsJob:
    """A job-pause row whose job ended without the farm seeing its terminal has no other
    exit (no plate verb answers a job pause), so it would block dispatch and stand every
    resume lane aside on its printer for good. The per-tick SWEEP closes it — after its
    dwell, under its own token — on the printer's POSITIVE report that the job is over.
    The startup rearm does not: the downtime reconcile needs the row open to classify a
    job stopped during the outage as a refused plate."""

    def _ctx(self, *, live, job=_JOB) -> Context:
        state = _state(live)
        state.subtask_id = job
        return Context(state=state, ledger=MotionLedger(), driver_live=False)

    @pytest.mark.parametrize("live", ["IDLE", "FINISH", "FAILED"])
    def test_a_terminal_or_idle_printer_ends_it_after_the_dwell(self, live):
        verdict = resolve(_row(RESOLUTION_JOB_PAUSE), "sweep_tick", self._ctx(live=live))

        assert (verdict.close, verdict.dwell, verdict.source) == (True, True, RESOLVE_JOB_ENDED_UNSEEN)
        assert live in verdict.evidence

    @pytest.mark.parametrize("live", ["RUNNING", "PAUSE", "PREPARE", "IDLE"])
    def test_another_job_on_the_printer_ends_it_after_the_dwell(self, live):
        """A printer runs one job; another one means this one ended."""
        verdict = resolve(_row(RESOLUTION_JOB_PAUSE), "sweep_tick", self._ctx(live=live, job="other-job"))

        assert (verdict.close, verdict.dwell, verdict.source) == (True, True, RESOLVE_JOB_ENDED_UNSEEN)

    def test_the_paused_job_running_again_keeps_its_own_token(self):
        """The RUNNING-of-that-job close is unchanged: it is an ANSWER, not an unseen end."""
        verdict = resolve(_row(RESOLUTION_JOB_PAUSE), "sweep_tick", self._ctx(live="RUNNING"))
        assert (verdict.close, verdict.source) == (True, RESOLVE_OBSERVED_RUNNING)

    @pytest.mark.parametrize("live", ["IDLE", "FINISH", "FAILED"])
    def test_startup_still_closes_only_on_running(self, live):
        assert resolve(_row(RESOLUTION_JOB_PAUSE), "startup", self._ctx(live=live)).close is False
        assert resolve(_row(RESOLUTION_JOB_PAUSE), "startup", self._ctx(live=live, job="other-job")).close is False
        assert resolve(_row(RESOLUTION_JOB_PAUSE), "startup", self._ctx(live="RUNNING")).close is True


class TestTheWireLane:
    def test_a_live_driver_defers_the_running_edge(self):
        """A RUNNING sample taken during a resume the DRIVER published is an
        intermediate reading of its procedure, not the hold being over (006-H2S
        2026-09-04: the row closed at the resume, the re-PAUSE found no open incident,
        and a second driver ran the swap round on the same AMS)."""
        ctx = Context(state=_state("RUNNING"), ledger=MotionLedger(), driver_live=True)

        verdict = resolve(_row(RESOLUTION_WIRE), "running_edge", ctx)

        assert verdict.close is False
        assert "closer stands aside" in verdict.evidence

    def test_an_orphaned_recovering_row_still_closes_on_the_running_edge(self):
        """The R1 orphan: a driver that crashed mid-recovery leaves ``recovering`` on
        a row nothing is acting on. The handover in ``_run_recovery``'s ``finally``
        re-reads the level once its slot is free, and that close must still land."""
        ctx = Context(state=_state("RUNNING"), ledger=MotionLedger(), driver_live=False)

        verdict = resolve(_row(RESOLUTION_WIRE, status=STATUS_RECOVERING), "running_edge", ctx)

        assert verdict.close is True
        assert verdict.source == RESOLVE_OBSERVED_RUNNING

    def test_a_live_driver_defers_the_job_terminal(self):
        """Review F2 (2026-09-23 wave): a live driver owns the outcome at a job terminal
        too. A release lever can END the print, and that terminal is a reading of the
        driver's own procedure — the driver records it (``ended``) and closes its row with
        its own source; the terminal closer must not free the row from under it."""
        ctx = Context(
            state=_state("IDLE"),
            ledger=MotionLedger(),
            driver_live=True,
            terminal=TerminalEvent(status="failed", eject=False, job_id=_JOB),
        )

        verdict = resolve(_row(RESOLUTION_WIRE, status=STATUS_RECOVERING), "job_terminal", ctx)

        assert (verdict.close, verdict.source) == (False, None)
        assert "closer stands aside" in verdict.evidence

    @pytest.mark.parametrize("status", [STATUS_RECOVERING, STATUS_ESCALATED])
    def test_with_no_live_driver_the_job_terminal_closes_the_wire_hold(self, status):
        """Liveness is the test, not the row's status: a ``recovering`` row whose driver
        died (the R1 orphan) has no owner left, and its job's terminal still closes it."""
        ctx = Context(
            state=_state("IDLE"),
            ledger=MotionLedger(),
            driver_live=False,
            terminal=TerminalEvent(status="failed", eject=False, job_id=_JOB),
        )

        verdict = resolve(_row(RESOLUTION_WIRE, status=status), "job_terminal", ctx)

        assert (verdict.close, verdict.source) == (True, RESOLVE_TERMINAL)

    def test_both_wire_edges_stand_aside_on_the_same_sentence(self):
        """One ownership rule at the two told-occasions a driver's own verb can cause, so
        the closers' two log lines read the same reason."""
        ctx = Context(
            state=_state("RUNNING"),
            ledger=MotionLedger(),
            driver_live=True,
            terminal=TerminalEvent(status="completed", eject=False, job_id=_JOB),
        )

        running = resolve(_row(RESOLUTION_WIRE), "running_edge", ctx)
        terminal = resolve(_row(RESOLUTION_WIRE), "job_terminal", ctx)

        assert running == terminal
        assert running.close is False

    @pytest.mark.parametrize("live", ["", "UNKNOWN", "PAUSE"])
    def test_a_non_positive_state_never_closes_a_wire_hold(self, live):
        """``""``/``UNKNOWN`` are absence of evidence; ``PAUSE`` IS the hold."""
        ctx = Context(state=_state(live), ledger=MotionLedger(), driver_live=False)

        assert resolve(_row(RESOLUTION_WIRE), "sweep_tick", ctx).close is False
        assert resolve(_row(RESOLUTION_WIRE), "startup", ctx).close is False

    def test_a_standing_sibling_fault_keeps_a_wire_hold(self):
        """Whole-printer, never per-code: #60 carried the pair ``0700_8006`` +
        ``0700_0006``, so a close keyed on the row's own code would have ended the hold
        with its sibling still standing."""
        ctx = Context(state=_state("IDLE", hms=[_ptfe_breakage_hms()]), ledger=MotionLedger(), driver_live=False)

        assert resolve(_row(RESOLUTION_WIRE), "sweep_tick", ctx).close is False

    def test_the_startup_cell_needs_no_fault_liveness_guard(self):
        """Deliberate asymmetry: a restart re-derives every wire fact from scratch, and
        a printer already positive has answered the question."""
        ctx = Context(state=_state("RUNNING", hms=[_ptfe_breakage_hms()]), ledger=MotionLedger(), driver_live=False)

        verdict = resolve(_row(RESOLUTION_WIRE), "startup", ctx)

        assert (verdict.close, verdict.source, verdict.dwell) == (True, RESOLVE_REARM, False)


class TestTheRepairLaneMotionEvidence:
    def _ctx(self, *, live="IDLE", hms=None, load_at=None, ams_status_main=0):
        return Context(
            state=_state(live, hms=hms, ams_status_main=ams_status_main),
            ledger=_ledger_with(load_at=load_at),
            driver_live=False,
        )

    def test_an_idle_clean_printer_is_not_evidence(self):
        """THE 003-H2S pin: the firmware wipes its standing HMS list at every terminal,
        so a printer with filament still jammed in its PTFE path reads exactly like a
        repaired one. Silence is not evidence; motion is."""
        assert resolve(_row(RESOLUTION_REPAIR), "sweep_tick", self._ctx()).close is False

    def test_a_completed_load_after_the_fault_closes_it(self):
        ctx = self._ctx(load_at=_OPENED_AT + timedelta(seconds=30))

        verdict = resolve(_row(RESOLUTION_REPAIR), "sweep_tick", ctx)

        assert (verdict.close, verdict.source, verdict.dwell) == (True, RESOLVE_REPAIR_OBSERVED, True)
        assert verdict.evidence == incident_resolution._REPAIR_EVIDENCE_LOAD  # noqa: SLF001

    def test_a_load_before_the_fault_is_the_one_that_jammed(self):
        ctx = self._ctx(load_at=_OPENED_AT - timedelta(seconds=30))

        assert resolve(_row(RESOLUTION_REPAIR), "sweep_tick", ctx).close is False

    def test_a_running_print_with_no_eject_closes_it(self):
        verdict = resolve(_row(RESOLUTION_REPAIR), "sweep_tick", self._ctx(live="RUNNING"))

        assert verdict.source == RESOLVE_REPAIR_OBSERVED
        assert verdict.evidence == incident_resolution._REPAIR_EVIDENCE_RUNNING  # noqa: SLF001

    def test_an_eject_sweep_is_not_a_print_through_the_path(self):
        """A sweep is filament-LESS: a toolhead crossing the plate says nothing about
        whether filament can be fed."""
        plate_occupancy.hydrate_plate(7, _JOB, EscalationOnly())
        plate_occupancy.hydrate_eject(
            7,
            PendingEject(
                purpose="manual",
                run_id=None,
                queue_item_id=None,
                dispatched_at=datetime.now(timezone.utc),
                started_at=None,
                hydrated=True,
            ),
        )

        assert resolve(_row(RESOLUTION_REPAIR), "sweep_tick", self._ctx(live="RUNNING")).close is False

    @pytest.mark.parametrize("live", ["", "UNKNOWN"])
    def test_a_printer_that_has_not_reported_closes_nothing(self, live):
        """A hold is not ended on a printer we cannot hear, however good the memory of
        its last load."""
        ctx = self._ctx(live=live, load_at=_OPENED_AT + timedelta(seconds=30))

        assert resolve(_row(RESOLUTION_REPAIR), "sweep_tick", ctx).close is False

    def test_evidence_is_not_a_latch_a_re_stall_keeps_the_hold(self):
        ctx = self._ctx(hms=[_ptfe_breakage_hms()], load_at=_OPENED_AT + timedelta(seconds=30))

        assert resolve(_row(RESOLUTION_REPAIR), "sweep_tick", ctx).close is False

    def test_a_wedged_ams_keeps_the_hold(self):
        """A wedge outlives the code that caused it: a fault-free wire with the AMS mid-change keeps the hold."""
        ctx = self._ctx(ams_status_main=1, load_at=_OPENED_AT + timedelta(seconds=30))

        assert resolve(_row(RESOLUTION_REPAIR), "sweep_tick", ctx).close is False

    def test_the_startup_cell_takes_the_same_evidence_with_no_dwell(self):
        ctx = self._ctx(live="RUNNING")

        verdict = resolve(_row(RESOLUTION_REPAIR), "startup", ctx)

        assert (verdict.close, verdict.source, verdict.dwell) == (True, RESOLVE_REARM, False)

    def test_recover_closes_it_and_a_routine_clear_plate_does_not(self):
        """The two plate verbs are different statements. "The bed is empty" says
        nothing about the filament path; "I inspected this machine" is the class's
        third return-to-normal."""
        base = Context(state=_state(), ledger=MotionLedger(), driver_live=False)

        def _verdict(*, recover: bool):
            return resolve(
                _row(RESOLUTION_REPAIR),
                "plate_cleared",
                replace(base, cleared=ClearedEvent(recover=recover)),
            )

        assert _verdict(recover=False).close is False
        recovered = _verdict(recover=True)
        assert (recovered.close, recovered.source) == (True, RESOLVE_OPERATOR)


class TestTheCompletedArm:
    """The 011-H2S evidence: the job the fault interrupted ran to ``completed``.

    23 of the 40 physical rows in this farm's history ended as a hand repair plus a
    resume, and the resumed job then ran to completion — filament fed to the end of it.
    Every qualification below closes a way that reading could be wrong.
    """

    def _ctx(self, *, status="completed", eject=False, job_id=_JOB, running_at="after", hms=None):
        seen = {
            "after": _OPENED_AT + timedelta(seconds=30),
            "before": _OPENED_AT - timedelta(seconds=30),
            None: None,
        }[running_at]
        return Context(
            state=_state("FINISH", hms=hms),
            ledger=_ledger_with(ran_at=seen),
            driver_live=False,
            terminal=TerminalEvent(status=status, eject=eject, job_id=job_id),
        )

    def test_the_same_job_completing_closes_it(self):
        verdict = resolve(_row(RESOLUTION_REPAIR), "job_terminal", self._ctx())

        assert (verdict.close, verdict.source, verdict.dwell) == (True, RESOLVE_REPAIR_COMPLETED, False)
        assert verdict.evidence == incident_resolution._REPAIR_EVIDENCE_COMPLETED  # noqa: SLF001

    def test_another_job_completing_launders_nothing(self):
        """The row blocks the DISPATCHER, not the touchscreen: a screen-started print
        completing on another slot must not clear a blocked shared path."""
        assert resolve(_row(RESOLUTION_REPAIR), "job_terminal", self._ctx(job_id="other-job")).close is False

    def test_a_terminal_that_names_no_job_launders_nothing(self):
        """A degenerate screen-restart echo names no subtask at all, and the row's own
        ``job_id`` defaults to ``''`` — the two must not match by emptiness."""
        row = _row(RESOLUTION_REPAIR)
        row.job_id = ""

        assert resolve(row, "job_terminal", self._ctx(job_id=None)).close is False

    @pytest.mark.parametrize("status", ["aborted", "failed", "cancelled", "FINISH", ""])
    def test_only_completed_counts(self, status):
        """Shape 38's pin, unchanged: a stop / failure / abort is very often the
        operator ending the very print the fault broke."""
        assert resolve(_row(RESOLUTION_REPAIR), "job_terminal", self._ctx(status=status)).close is False

    def test_a_completed_eject_sweep_is_not_repair_evidence(self):
        """The sweep is filament-less, and it completes on every production cycle — so
        without this the farm's own eject would launder every physical hold it met."""
        assert resolve(_row(RESOLUTION_REPAIR), "job_terminal", self._ctx(eject=True)).close is False

    def test_no_running_sighting_after_the_fault_stands(self):
        """The disqualifier for a pull-back timeout raised at END-OF-PRINT unload: the
        job completes, but nothing ever ran through the path after the fault."""
        assert resolve(_row(RESOLUTION_REPAIR), "job_terminal", self._ctx(running_at=None)).close is False

    def test_a_running_sighting_from_BEFORE_the_row_stands(self):
        """The print that was running when the fault arrived is the one it interrupted."""
        assert resolve(_row(RESOLUTION_REPAIR), "job_terminal", self._ctx(running_at="before")).close is False

    def test_a_fault_still_standing_at_the_terminal_stands(self):
        """Evidence is not a latch here either."""
        ctx = self._ctx(hms=[_ptfe_breakage_hms()])

        assert resolve(_row(RESOLUTION_REPAIR), "job_terminal", ctx).close is False

    def test_a_wire_row_still_closes_on_any_terminal(self):
        """The completed arm is the REPAIR class's. A wire hold is over when its job
        ends, whatever the outcome — that rule is untouched."""
        verdict = resolve(_row(RESOLUTION_WIRE), "job_terminal", self._ctx(status="aborted", running_at=None))

        assert (verdict.close, verdict.source) == (True, RESOLVE_TERMINAL)


class TestMotionLedger:
    def _sample(self, ledger, printer_id, *, tray_now, epoch=1, live="IDLE"):
        ledger.observe(printer_id, _state(live, tray_now=tray_now, epoch=epoch), epoch)

    def test_the_first_sample_only_seeds(self):
        """An edge that happened before we looked is not an edge we witnessed."""
        ledger = MotionLedger()
        self._sample(ledger, 7, tray_now=1)

        assert ledger.load_completed_at(7) is None

    def test_a_transition_onto_a_real_feeder_stamps(self):
        ledger = MotionLedger()
        self._sample(ledger, 7, tray_now=255)
        self._sample(ledger, 7, tray_now=1)

        assert ledger.load_completed_at(7) is not None

    def test_a_standing_level_never_stamps(self):
        """003-H2S read ``tray_now == 1`` before, during and after its fault."""
        ledger = MotionLedger()
        for _ in range(3):
            self._sample(ledger, 7, tray_now=1)

        assert ledger.load_completed_at(7) is None

    @pytest.mark.parametrize("sentinel", [254, 255])
    def test_a_sentinel_is_not_a_feeder(self, sentinel):
        """254 is the external holder and 255 is "nothing is feeding" — neither is
        something a load can complete ONTO."""
        ledger = MotionLedger()
        self._sample(ledger, 7, tray_now=1)
        self._sample(ledger, 7, tray_now=sentinel)

        assert ledger.load_completed_at(7) is None

    def test_an_epoch_change_cannot_fabricate_a_load_edge(self):
        """A new MQTT session re-seeds every wire fact at once, so a tray "changing"
        across it is a reading, not an event."""
        ledger = MotionLedger()
        self._sample(ledger, 7, tray_now=255, epoch=1)
        self._sample(ledger, 7, tray_now=1, epoch=2)

        assert ledger.load_completed_at(7) is None

    def test_running_is_stamped_on_the_positive_reading_with_no_edge(self):
        """No seed, no epoch test: "this printer was demonstrably printing" is a fact a
        reconnect cannot fabricate, and it must be usable on the FIRST push after one."""
        ledger = MotionLedger()
        self._sample(ledger, 7, tray_now=1, live="RUNNING")

        assert ledger.path_ran_at(7) is not None

    @pytest.mark.parametrize("live", ["IDLE", "PAUSE", "FINISH", "PREPARE", ""])
    def test_only_running_stamps_the_sighting(self, live):
        ledger = MotionLedger()
        self._sample(ledger, 7, tray_now=1, live=live)

        assert ledger.path_ran_at(7) is None

    def test_a_running_eject_sweep_never_enters_the_ledger(self):
        """The eject exclusion is asked at STAMP time for this fact, so a sweep's
        RUNNING can never become the "a print ran through the path" evidence the
        completed arm reads back hours later."""
        plate_occupancy.hydrate_plate(7, _JOB, EscalationOnly())
        plate_occupancy.hydrate_eject(
            7,
            PendingEject(
                purpose="manual",
                run_id=None,
                queue_item_id=None,
                dispatched_at=datetime.now(timezone.utc),
                started_at=None,
                hydrated=True,
            ),
        )
        ledger = MotionLedger()
        self._sample(ledger, 7, tray_now=1, live="RUNNING")

        assert ledger.path_ran_at(7) is None

    def test_the_ledger_is_per_printer(self):
        ledger = MotionLedger()
        self._sample(ledger, 7, tray_now=255)
        self._sample(ledger, 7, tray_now=1)

        assert ledger.load_completed_at(8) is None

    def test_reset_drops_everything(self):
        """``spool_recovery._reset_state`` delegates here — no second test fixture."""
        ledger = MotionLedger()
        self._sample(ledger, 7, tray_now=255)
        self._sample(ledger, 7, tray_now=1, live="RUNNING")
        assert ledger.load_completed_at(7) is not None
        assert ledger.path_ran_at(7) is not None

        ledger.reset()

        assert ledger.load_completed_at(7) is None
        assert ledger.path_ran_at(7) is None
        # ...including the feeder memory, so the next sample seeds rather than edges.
        self._sample(ledger, 7, tray_now=2)
        assert ledger.load_completed_at(7) is None


class _LiveTask:
    """A recovery driver's liveness-slot occupant that has not finished — all the store's
    ``driver_live`` asks of a task is ``done()``."""

    def done(self) -> bool:
        return False


@pytest.fixture
def live_driver():
    """A recovery driver LIVE on printer 7 for the test's duration, through the store's own
    ``register_driver`` / ``release_driver`` (the one liveness store)."""
    task = _LiveTask()
    printer_incidents.register_driver(7, task, incident_id=1)
    yield task
    printer_incidents.release_driver(7, task)


class TestTheSightingIsQualified:
    """``path_ran_at`` is "a print ran THROUGH THE PATH", qualified when it is WRITTEN — the
    reading is used long after the sample, when the printer's state then says nothing
    about the state at the sample. Each exclusion below is a RUNNING reading that is not
    filament feeding through a repaired path."""

    @staticmethod
    def _sample(ledger, *, hms=None, ams_status_main=0):
        ledger.observe(7, _state("RUNNING", hms=hms, tray_now=1, ams_status_main=ams_status_main), 1)

    def test_a_quiet_running_print_with_no_driver_is_a_sighting(self):
        ledger = MotionLedger()
        self._sample(ledger)

        assert ledger.path_ran_at(7) is not None

    def test_running_with_a_fault_standing_is_not(self):
        """The fault-before-PAUSE window: the row opens on the HMS push, BEFORE the PAUSE
        lands, and the samples between carry the very fault it is about."""
        ledger = MotionLedger()
        self._sample(ledger, hms=[_ptfe_breakage_hms()])

        assert ledger.path_ran_at(7) is None

    def test_running_with_the_ams_mid_change_is_not(self):
        """A wedge outlives its code: a fault-free wire with the AMS mid-change is not quiet."""
        ledger = MotionLedger()
        self._sample(ledger, ams_status_main=1)

        assert ledger.path_ran_at(7) is None

    def test_running_under_a_live_recovery_driver_is_not(self, live_driver):
        """A driver's own lever resume: the running-edge cells' rule — a RUNNING sample taken
        during a resume the driver published is its intermediate reading."""
        ledger = MotionLedger()
        self._sample(ledger)

        assert ledger.path_ran_at(7) is None

    def test_the_driver_releasing_lets_the_next_quiet_sample_count(self, live_driver):
        ledger = MotionLedger()
        self._sample(ledger)
        printer_incidents.release_driver(7, live_driver)

        self._sample(ledger)

        assert ledger.path_ran_at(7) is not None


def _pull_back_hms() -> list[HMSError]:
    """002-H2S's pull-back pair, one per wire lane: ``0700_0011`` (hms[], AMS A slot 3 —
    "pull-back timeout") and ``0700_8004`` (print_error — "failed to pull back")."""
    attr = 0x07000000 | ((0x20 + 2) << 8)
    return [
        HMSError(code="0x20011", attr=attr, module=7, severity=2, full_code=f"{attr:08X}00020011"),
        HMSError(code="8004", attr=0x07008004, module=7, severity=2, full_code="07008004"),
    ]


def _extruder_overload_hms() -> HMSError:
    """``0300_801E`` — the fault 006-H2S's incident 289 swallowed on 2026-09-21."""
    return HMSError(code="801E", attr=0x03000000, module=3, severity=2)


def _physical_row(opened_on: list[HMSError], *, created_at: datetime) -> PrinterIncident:
    """An open AMS-side PHYSICAL row (the ``repair`` class) whose ``codes`` are the entry
    gate's own fingerprint of the faults it opened on."""
    return PrinterIncident(
        id=1,
        printer_id=7,
        job_id=_JOB,
        item_id=None,
        kind=KIND_PHYSICAL,
        code="0700_0011",
        codes=candidate_fingerprint(live_candidates(_state(hms=opened_on))),
        slot_global_tray=2,
        status=STATUS_ESCALATED,
        created_at=created_at,
    )


def _opened_minutes_ago() -> datetime:
    """``created_at`` safely before any sighting this case then writes (the ledger stamps
    the wall clock at ``observe``)."""
    return datetime.utcnow() - timedelta(minutes=5)


class TestTheNewFaultOccasion:
    """The AMS entry gate asks the open AMS row whether the fault now on the wire ENDS it.

    002-H2S 2026-09-15/16: three ``0700_8010`` jams swallowed behind ``0700_0011`` +
    ``0700_8004`` pull-back rows; 006-H2S 2026-09-21: a ``0300_801E`` behind incident 289.
    The rework made the RUNNING edge no repair evidence for the class, so the rows
    outlived the resume that had repaired the path, and the entry refused every new fault
    as "already held". A repair row now closes ``repair_observed`` on a new fault once a
    print demonstrably RAN through the path after it opened — and on nothing weaker.

    Every sighting is written the way production writes it, through
    ``MotionLedger.observe`` (the sampler's own call), never pre-seeded.
    """

    @staticmethod
    def _ran(ledger: MotionLedger, *, hms=None) -> None:
        """One RUNNING push through the sampler's call (qualified or not is the ledger's)."""
        ledger.observe(7, _state("RUNNING", hms=hms, tray_now=1), 1)

    @staticmethod
    def _now_on_wire(ledger: MotionLedger, hms, *, live="PAUSE", driver_live=False) -> Context:
        return Context(state=_state(live, hms=hms, tray_now=1), ledger=ledger, driver_live=driver_live)

    @pytest.mark.parametrize(
        "new_fault",
        [pytest.param(_jam_hms, id="002-H2S-0700_8010"), pytest.param(_extruder_overload_hms, id="006-H2S-0300_801E")],
    )
    def test_a_new_fault_after_a_print_ran_through_the_path_closes_it(self, new_fault):
        ledger = MotionLedger()
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())
        self._ran(ledger)  # the operator repaired the path and resumed; it printed

        verdict = resolve(row, "new_fault", self._now_on_wire(ledger, [new_fault()], live="RUNNING"))

        assert (verdict.close, verdict.source, verdict.dwell) == (True, RESOLVE_REPAIR_OBSERVED, False)
        assert verdict.evidence == incident_resolution._REPAIR_EVIDENCE_NEW_FAULT  # noqa: SLF001

    def test_the_rows_own_fault_still_standing_keeps_it(self):
        """A new code BESIDE the row's own is the same blockage, better described — the
        entry's outrank test is what re-classifies a row, never this close."""
        ledger = MotionLedger()
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())
        self._ran(ledger)

        verdict = resolve(row, "new_fault", self._now_on_wire(ledger, [*_pull_back_hms(), _jam_hms()]))

        assert verdict.close is False
        assert "own fault still stands" in verdict.evidence

    @pytest.mark.parametrize("lane", [0, 1], ids=["hms-lane-0700_0011", "print-error-lane-0700_8004"])
    def test_one_own_code_on_either_wire_lane_keeps_it(self, lane):
        """Both lanes are the row's own words: either one standing is the old fault."""
        ledger = MotionLedger()
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())
        self._ran(ledger)

        verdict = resolve(row, "new_fault", self._now_on_wire(ledger, [_pull_back_hms()[lane], _jam_hms()]))

        assert verdict.close is False

    def test_the_same_code_on_another_slot_is_a_different_fault(self):
        """The comparison is the entry gate's own slot-qualified token: a second slot's
        breakage is a new fault, exactly as a second roll running dry is a new runout."""
        ledger = MotionLedger()
        row = _physical_row([_ptfe_breakage_hms(tray_id=3)], created_at=_opened_minutes_ago())
        self._ran(ledger)

        verdict = resolve(row, "new_fault", self._now_on_wire(ledger, [_ptfe_breakage_hms(tray_id=1)]))

        assert (verdict.close, verdict.source) == (True, RESOLVE_REPAIR_OBSERVED)

    def test_no_actionable_fault_is_no_new_fault(self):
        ledger = MotionLedger()
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())
        self._ran(ledger)

        verdict = resolve(row, "new_fault", self._now_on_wire(ledger, []))

        assert verdict.close is False

    def test_a_sighting_from_a_drivers_own_resume_does_not_count(self, live_driver):
        """A RUNNING sample during a resume a live driver published is its reading — on an
        UPGRADED row too, whose ``created_at`` is the jam's."""
        ledger = MotionLedger()
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())
        self._ran(ledger)  # the driver's lever resume: RUNNING, quiet, driver live
        printer_incidents.release_driver(7, live_driver)  # it handed over

        verdict = resolve(row, "new_fault", self._now_on_wire(ledger, [_jam_hms()]))

        assert verdict.close is False
        assert "no print ran through the path" in verdict.evidence

    def test_the_fault_before_pause_window_does_not_count(self):
        """The row opens on the HMS push, BEFORE the PAUSE lands; the RUNNING samples in
        between carry the very fault it is about — a sighting there is not the path running."""
        ledger = MotionLedger()
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())
        self._ran(ledger, hms=_pull_back_hms())
        self._ran(ledger, hms=_pull_back_hms())

        verdict = resolve(row, "new_fault", self._now_on_wire(ledger, [_jam_hms()]))

        assert verdict.close is False
        assert "no print ran through the path" in verdict.evidence

    def test_a_sighting_from_before_the_row_opened_does_not_count(self):
        """The print that was running when the fault arrived is the one it interrupted."""
        ledger = MotionLedger()
        self._ran(ledger)
        row = _physical_row(_pull_back_hms(), created_at=ledger.path_ran_at(7) + timedelta(seconds=1))

        assert resolve(row, "new_fault", self._now_on_wire(ledger, [_jam_hms()])).close is False

    def test_a_live_driver_at_the_verdict_stands_aside(self):
        """The told-occasion ownership rule: closing a row from under a live driver would let
        the entry spawn a second driver onto one AMS (006-H2S 2026-09-04)."""
        ledger = MotionLedger()
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())
        self._ran(ledger)

        verdict = resolve(row, "new_fault", self._now_on_wire(ledger, [_jam_hms()], driver_live=True))

        assert verdict.close is False
        assert "closer stands aside" in verdict.evidence

    def test_after_a_restart_the_row_stands_until_this_process_sees_the_path_run(self):
        """The ledger is process memory, and a restart empties it (``ledger.reset`` is what a
        restart leaves). With no sighting of its own the process never ends a hold on motion
        it did not witness — the SAFE direction: the row stands and the new fault waits.
        The first quiet RUNNING the new process samples is the evidence again."""
        ledger = MotionLedger()
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())
        self._ran(ledger)
        ledger.reset()  # the restart

        assert resolve(row, "new_fault", self._now_on_wire(ledger, [_jam_hms()])).close is False

        self._ran(ledger)  # after the restart, the printer prints on a quiet path

        assert resolve(row, "new_fault", self._now_on_wire(ledger, [_jam_hms()])).close is True


class TestTheCompletedArmReadsTheSharedSighting:
    """``_repair_job_terminal`` and the new-fault arm read ONE predicate of "a print ran
    through the path" (``_ran_through_path_since``, over the qualified sighting). These
    fail if the completed arm read a bare RUNNING stamp: each writes a RUNNING sample the
    old stamp recorded and the qualified one does not."""

    @staticmethod
    def _completed(ledger: MotionLedger, row: PrinterIncident):
        return resolve(
            row,
            "job_terminal",
            Context(
                state=_state("FINISH"),
                ledger=ledger,
                driver_live=False,
                terminal=TerminalEvent(status="completed", eject=False, job_id=_JOB),
            ),
        )

    def test_a_qualified_sighting_lets_the_completion_close_it(self):
        ledger = MotionLedger()
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())
        ledger.observe(7, _state("RUNNING", tray_now=1), 1)

        verdict = self._completed(ledger, row)

        assert (verdict.close, verdict.source) == (True, RESOLVE_REPAIR_COMPLETED)

    def test_a_drivers_own_resume_does_not_launder_the_completion(self, live_driver):
        ledger = MotionLedger()
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())
        ledger.observe(7, _state("RUNNING", tray_now=1), 1)
        printer_incidents.release_driver(7, live_driver)

        assert self._completed(ledger, row).close is False

    def test_running_with_the_fault_standing_does_not_launder_it_either(self):
        """The fault-before-PAUSE samples, then the firmware wipes its list at the terminal."""
        ledger = MotionLedger()
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())
        ledger.observe(7, _state("RUNNING", hms=_pull_back_hms(), tray_now=1), 1)

        assert self._completed(ledger, row).close is False


class TestDriverOwns:
    """ONE spelling of "a driver owns this row", replacing two that had drifted."""

    @pytest.mark.parametrize(
        ("status", "live", "expected"),
        [
            (STATUS_RECOVERING, False, True),  # the row PROMISES a task is acting
            (STATUS_RECOVERING, True, True),
            (STATUS_ESCALATED, True, True),  # the task slot says one is
            (STATUS_ESCALATED, False, False),  # nobody — this row is adjudicable
        ],
    )
    def test_driver_owns(self, status, live, expected):
        assert driver_owns(_row(RESOLUTION_WIRE, status=status), live=live) is expected


class TestPathQuiet:
    def test_a_clean_wire_is_quiet(self):
        assert path_quiet(_state("IDLE")) is True

    def test_an_actionable_fault_is_not(self):
        assert path_quiet(_state("IDLE", hms=[_ptfe_breakage_hms()])) is False

    def test_a_wedged_ams_is_not(self):
        assert path_quiet(_state("IDLE", ams_status_main=1)) is False

    def test_no_state_at_all_is_quiet(self):
        """Deliberate: absence of a reading is not a fault. The callers that must not
        act on a silent printer guard on the STATE, not on this."""
        assert path_quiet(None) is True
