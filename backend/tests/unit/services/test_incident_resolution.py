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
* **the repair evidence is the JOB'S OWN RECORD and the record's** — a load nobody commanded, a
  layer printed fed past the row's ``fault_layer`` — never process memory a restart empties, and no
  cell waits out a window (2026-10-11).
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models.printer_incident import (
    KIND_JAM,
    KIND_PHYSICAL,
    KIND_PLATE_VISION,
    KIND_POWER_LOSS,
    KIND_RUNOUT,
    KIND_SERVICE_HOLD,
    KIND_TOOLHEAD_REFILL,
    KIND_Z_REFERENCE_LOST,
    RESOLUTION_DECLARED,
    RESOLUTION_JOB_PAUSE,
    RESOLUTION_OPERATOR,
    RESOLUTION_REPAIR,
    RESOLUTION_TOOLHEAD,
    RESOLUTION_WIRE,
    RESOLVE_JOB_ENDED_UNSEEN,
    RESOLVE_OBSERVED_RUNNING,
    RESOLVE_OPERATOR,
    RESOLVE_PLATE_REFUSED,
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
    TerminalEvent,
    driver_owns,
    path_quiet,
    resolve,
)
from backend.app.services.plate_occupancy import EscalationOnly, PendingEject, plate_occupancy
from backend.tests._fixtures.feed import ScriptedFeed

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
    RESOLUTION_TOOLHEAD: (KIND_TOOLHEAD_REFILL, ""),
}

_JOB = "job-1"
_OPENED_AT = datetime(2026, 9, 17, 9, 43, 0)


@pytest.fixture(autouse=True)
def _clean_authority():
    """The eject exclusion asks the plate authority, which is process state."""
    plate_occupancy.reset_for_tests()
    yield
    plate_occupancy.reset_for_tests()


# The job layer the fault opened a row at (``PrinterIncident.fault_layer``); ``_state`` reports layer 10.
_FAULT_LAYER = 5


def _row(
    resolution: str, *, status: str = STATUS_ESCALATED, printer_id: int = 7, fault_layer: int | None = _FAULT_LAYER
) -> PrinterIncident:
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
        fault_layer=fault_layer,
    )


def _ptfe_breakage_hms(ams_id: int = 0, tray_id: int = 3) -> HMSError:
    """``0700_0006`` — an ACTIONABLE physical fault, i.e. the path is not quiet."""
    attr = 0x07000000 | (ams_id << 16) | ((0x20 + tray_id) << 8)
    return HMSError(code="0x20006", attr=attr, module=7, severity=2, full_code=f"{attr:08X}00020006")


def _state(
    live: str = "IDLE",
    *,
    hms=None,
    tray_now=None,
    epoch=1,
    ams_status_main=0,
    snow=None,
    active=0,
    layer=10,
    absent=(),
) -> PrinterState:
    """A LIVE reading of the printer (its session's report applied) at a printed ``layer``.
    ``snow`` is the dual-nozzle per-extruder map (``{extruder_id: global_tray}``) and ``active``
    the active extruder — a single-nozzle state when ``snow`` is None; ``absent`` names the
    AMS-0 trays that read empty (every other tray of the four reads seated)."""
    st = PrinterState()
    st.connected = True
    st.state = live
    st.subtask_id = _JOB
    st.hms_errors = hms or []
    st.tray_now = tray_now
    st.connection_epoch = epoch
    st.report_epoch = epoch
    st.ams_status_main = ams_status_main
    st.h2d_extruder_snow = dict(snow or {})
    st.active_extruder = active
    st.layer_num = layer
    st.raw_data = {
        "ams": [
            {
                "id": 0,
                "tray": [
                    {"id": i, "state": 9, "tray_type": ""}
                    if i in absent
                    else {"id": i, "state": 11, "tray_type": "PETG"}
                    for i in range(4)
                ],
            }
        ]
    }
    return st


def _feed_of(state: PrinterState | None):
    """The printer client's toolhead FEED STATE once it has seen ``state`` reported (a real tracker
    stepped off the scripted pushes: the session's re-seed, then the reading) — what every caller
    hands the rule table as ``Context.feed`` (invariant 16). ``None`` for no state (no client)."""
    if state is None:
        return None
    return ScriptedFeed().settle(state)


def _ctx_of(state: PrinterState | None, **kw) -> Context:
    """A verdict's context over a live reading: the client's feed state for it and the tracked job's
    layer (``Context.job_layer`` — the state's own), as every closer hands them."""
    return Context(
        state=state,
        feed=_feed_of(state),
        job_layer=None if state is None else state.layer_num,
        **{"driver_live": False, **kw},
    )


def _air_feed(*, layer: int = 10):
    """A printer's feed state after its job printed a layer with NOTHING fed (``first_unfed_layer``
    stamped — invariant 16's AIR): RUNNING, ``tray_now`` 255, the layer advancing once."""
    from backend.app.services.feed_state import JobBoundary

    def _push(tray_now: int, at: int) -> PrinterState:
        state = _state("RUNNING", tray_now=tray_now, layer=at)
        state.total_layers = 100
        return state

    feed = ScriptedFeed()
    feed.push(_push(1, 1), boundary=JobBoundary.NEW_JOB)  # the job this client watched start
    feed.push(_push(255, layer))
    feed.push(_push(255, layer + 1))  # a layer advanced with nothing fed: AIR
    reading = feed.push(_push(1, layer + 2))  # refilled, fed again
    assert reading.first_unfed_layer is not None
    return reading


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
    the paused job printing again for ``job_pause``, a print running FED for
    ``toolhead`` — the active extruder reads slot 1). For ``new_fault`` the wire also
    carries a DIFFERENT actionable fault than the row's own (its occasion's meaning).
    """
    after = _OPENED_AT + timedelta(seconds=30)
    state = _state("RUNNING", hms=[_jam_hms()] if occasion == "new_fault" else None, tray_now=0)
    return Context(
        state=state,
        observed_load_at=after,
        driver_live=False,
        terminal=TerminalEvent(status="completed", eject=False, job_id=_JOB, last_layer=20),
        cleared=ClearedEvent(recover=True),
        feed=_feed_of(state),
        job_layer=state.layer_num,
    )


# (class, occasion) -> the source it closes with, or None for "stand".
_EXPECTED: dict[tuple[str, str], str | None] = {
    (RESOLUTION_WIRE, "running_edge"): RESOLVE_OBSERVED_RUNNING,
    (RESOLUTION_WIRE, "job_terminal"): RESOLVE_TERMINAL,
    (RESOLUTION_WIRE, "sweep_tick"): RESOLVE_WIRE_CLEAR,
    (RESOLUTION_WIRE, "startup"): RESOLVE_REARM,
    (RESOLUTION_WIRE, "plate_cleared"): None,
    # R27: a wire row stays open over a RUNNING print until the layer advance, so a DIFFERENT fault
    # after the print ran on past it (layer 10 past the fault's 5 here) is its own incident.
    (RESOLUTION_WIRE, "new_fault"): RESOLVE_OBSERVED_RUNNING,
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
    # K10 (2026-10-10): an empty-toolhead hold ends when a print runs FED, when its job ends, or
    # on Recover — never on an AMS fault appearing (that fault opens its own row).
    (RESOLUTION_TOOLHEAD, "running_edge"): RESOLVE_OBSERVED_RUNNING,
    (RESOLUTION_TOOLHEAD, "job_terminal"): RESOLVE_TERMINAL,
    (RESOLUTION_TOOLHEAD, "sweep_tick"): RESOLVE_OBSERVED_RUNNING,
    (RESOLUTION_TOOLHEAD, "startup"): RESOLVE_REARM,
    (RESOLUTION_TOOLHEAD, "plate_cleared"): RESOLVE_OPERATOR,
    (RESOLUTION_TOOLHEAD, "new_fault"): None,
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

    def test_no_verdict_carries_a_window(self):
        """2026-10-11: no cell waits out a dwell — the verdict is close or stand, on an event."""
        assert not hasattr(incident_resolution.Verdict(close=True), "dwell")


class TestATerminalInFlightStandsTheLookingCells:
    """The printer reported its job's end and ``main.on_print_complete`` is still handling it: that
    terminal reads the open holds to classify the stop and the plate BEFORE it closes them, so the farm's
    LOOKING occasions (the sweep, the session start) stand until it returns — the event decides, where a
    120 s dwell used to keep it first. The told occasions are untouched."""

    @pytest.mark.parametrize(("resolution", "occasion"), sorted(_EXPECTED))
    def test_cell(self, resolution, occasion):
        verdict = resolve(_row(resolution), occasion, replace(_permissive(occasion), terminal_in_flight=True))
        looking = occasion in ("sweep_tick", "startup")
        expected_source = None if looking else _EXPECTED[(resolution, occasion)]

        assert verdict.close is (expected_source is not None), verdict.evidence
        assert verdict.source == expected_source
        if looking:
            assert "terminal is being handled" in verdict.evidence


class TestAPendingCommandStandsEveryCellButTheJobTerminalAndAPersonsClear:
    """K11 (011/014-H2S 2026-10-09/10): while the farm's own motion command on the row has not run
    (``Context.command_pending``, read off the row's step ledger), the row is the farm's to keep —
    the AMS can still run it on its own and empty the toolhead under the print — so EVERY cell
    stands, whatever the evidence, except two: the job terminal (a job that is over has no toolhead
    left to print air with) and a person's explicit clear (Recover / Mark plate cleared — a human
    declared the printer fixed; the next resume refills (T3) and a print run onto air is caught
    (T4)). The ``restart_owed`` precedent, widened from one cell to all but two."""

    @pytest.mark.parametrize(("resolution", "occasion"), sorted(_EXPECTED))
    def test_cell(self, resolution, occasion):
        verdict = resolve(_row(resolution), occasion, replace(_permissive(occasion), command_pending=True))
        exempt = occasion in ("job_terminal", "plate_cleared")
        expected_source = _EXPECTED[(resolution, occasion)] if exempt else None

        assert verdict.close is (expected_source is not None), verdict.evidence
        assert verdict.source == expected_source
        assert verdict.evidence

    def test_the_stand_says_why(self):
        verdict = resolve(
            _row(RESOLUTION_WIRE), "running_edge", replace(_permissive("running_edge"), command_pending=True)
        )

        assert "has not run" in verdict.evidence

    def test_an_unregistered_class_still_raises_first(self, monkeypatch):
        """The guard reads after the table: a hold nobody wrote a rule for still fails loudly."""
        monkeypatch.setattr(incident_resolution.printer_incidents, "resolution_class", lambda *a, **k: "invented")

        with pytest.raises(KeyError):
            resolve(_row(RESOLUTION_WIRE), "sweep_tick", replace(_permissive("sweep_tick"), command_pending=True))


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
        (KIND_TOOLHEAD_REFILL, False): "",
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
        return Context(state=state, driver_live=driver_live, terminal=terminal)

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

    def test_a_terminal_that_refused_the_plate_closes_it_under_its_own_source(self):
        """The terminal outcome's answer rides the event: a refusal is the human's (a gate
        and a page follow), so the ledger must tell it from the farm's own stop-and-retry."""
        refused = TerminalEvent(status="failed", eject=False, job_id=_JOB, plate_refused=True)
        retried = TerminalEvent(status="failed", eject=False, job_id=_JOB)

        verdict = resolve(_row(RESOLUTION_JOB_PAUSE), "job_terminal", self._ctx(live="FAILED", terminal=refused))
        assert (verdict.close, verdict.source) == (True, RESOLVE_PLATE_REFUSED)
        verdict = resolve(_row(RESOLUTION_JOB_PAUSE), "job_terminal", self._ctx(live="FAILED", terminal=retried))
        assert (verdict.close, verdict.source) == (True, RESOLVE_TERMINAL)

    def test_a_refusal_of_another_job_or_an_eject_still_stands(self):
        other = TerminalEvent(status="failed", eject=False, job_id="other-job", plate_refused=True)
        sweep = TerminalEvent(status="completed", eject=True, job_id=_JOB, plate_refused=True)
        assert resolve(_row(RESOLUTION_JOB_PAUSE), "job_terminal", self._ctx(terminal=other)).close is False
        assert resolve(_row(RESOLUTION_JOB_PAUSE), "job_terminal", self._ctx(terminal=sweep)).close is False

    def test_the_refusal_flag_changes_no_other_class(self):
        """Only the ``job_pause`` cell reads it: a wire hold's terminal close is unchanged."""
        refused = TerminalEvent(status="failed", eject=False, job_id=_JOB, plate_refused=True)
        verdict = resolve(_row(RESOLUTION_WIRE), "job_terminal", self._ctx(live="FAILED", terminal=refused))
        assert (verdict.close, verdict.source) == (True, RESOLVE_TERMINAL)

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
        ctx = Context(state=None, driver_live=False)
        assert resolve(_row(RESOLUTION_JOB_PAUSE), "sweep_tick", ctx).close is False

    @pytest.mark.parametrize("occasion", ["sweep_tick", "startup"])
    def test_an_eject_owning_the_printer_is_not_the_answer(self, occasion):
        """The shared "RUNNING and no eject owns it" reading — a sweep is not the paused job."""
        plate_occupancy.hydrate_eject(7, PendingEject(purpose="production", run_id=None, queue_item_id=1))
        assert resolve(_row(RESOLUTION_JOB_PAUSE), occasion, self._ctx()).close is False

    def test_the_sweep_and_the_session_start_close_on_the_job_running_again(self):
        """Both read the printer's report, neither waits out a window — only the source differs."""
        sweep = resolve(_row(RESOLUTION_JOB_PAUSE), "sweep_tick", self._ctx())
        startup = resolve(_row(RESOLUTION_JOB_PAUSE), "startup", self._ctx())
        assert (sweep.close, sweep.source) == (True, RESOLVE_OBSERVED_RUNNING)
        assert (startup.close, startup.source) == (True, RESOLVE_REARM)

    @pytest.mark.parametrize("recover", [True, False])
    def test_neither_plate_verb_answers_a_paused_job(self, recover):
        """ "The plate is clear" is not "resume the print"."""
        ctx = Context(state=_state("PAUSE"), driver_live=False, cleared=ClearedEvent(recover))
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
        return resolve(row, occasion, Context(state=state, driver_live=False))

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
    resume lane aside on its printer for good. The per-tick SWEEP closes it — under its own
    token — on the printer's POSITIVE report that the job is over (a terminal still being handled
    stands it aside, :class:`TestATerminalInFlightStandsTheLookingCells`). The startup rearm does not: the downtime reconcile needs the row open to classify a
    job stopped during the outage as a refused plate."""

    def _ctx(self, *, live, job=_JOB) -> Context:
        state = _state(live)
        state.subtask_id = job
        return Context(state=state, driver_live=False)

    @pytest.mark.parametrize("live", ["IDLE", "FINISH", "FAILED"])
    def test_a_terminal_or_idle_printer_ends_it(self, live):
        verdict = resolve(_row(RESOLUTION_JOB_PAUSE), "sweep_tick", self._ctx(live=live))

        assert (verdict.close, verdict.source) == (True, RESOLVE_JOB_ENDED_UNSEEN)
        assert live in verdict.evidence

    @pytest.mark.parametrize("live", ["RUNNING", "PAUSE", "PREPARE", "IDLE"])
    def test_another_job_on_the_printer_ends_it(self, live):
        """A printer runs one job; another one means this one ended."""
        verdict = resolve(_row(RESOLUTION_JOB_PAUSE), "sweep_tick", self._ctx(live=live, job="other-job"))

        assert (verdict.close, verdict.source) == (True, RESOLVE_JOB_ENDED_UNSEEN)

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
        ctx = Context(state=_state("RUNNING"), driver_live=True)

        verdict = resolve(_row(RESOLUTION_WIRE), "running_edge", ctx)

        assert verdict.close is False
        assert "closer stands aside" in verdict.evidence

    def test_an_orphaned_recovering_row_still_closes_on_the_running_edge(self):
        """The R1 orphan: a driver that crashed mid-recovery leaves ``recovering`` on
        a row nothing is acting on. The handover in ``_run_recovery``'s ``finally``
        re-reads the level once its slot is free, and that close must still land (on a print that has
        printed past the fault — R27)."""
        ctx = _ctx_of(_state("RUNNING", tray_now=1))

        verdict = resolve(_row(RESOLUTION_WIRE, status=STATUS_RECOVERING), "running_edge", ctx)

        assert verdict.close is True
        assert verdict.source == RESOLVE_OBSERVED_RUNNING

    def test_a_live_driver_defers_the_job_terminal(self):
        """A live driver owns the outcome at a job terminal
        too. A release lever can END the print, and that terminal is a reading of the
        driver's own procedure — the driver records it (``ended``) and closes its row with
        its own source; the terminal closer must not free the row from under it."""
        ctx = Context(
            state=_state("IDLE"),
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
        ctx = Context(state=_state(live), driver_live=False)

        assert resolve(_row(RESOLUTION_WIRE), "sweep_tick", ctx).close is False
        assert resolve(_row(RESOLUTION_WIRE), "startup", ctx).close is False

    def test_a_standing_sibling_fault_keeps_a_wire_hold(self):
        """Whole-printer, never per-code: #60 carried the pair ``0700_8006`` +
        ``0700_0006``, so a close keyed on the row's own code would have ended the hold
        with its sibling still standing."""
        ctx = Context(state=_state("IDLE", hms=[_ptfe_breakage_hms()]), driver_live=False)

        assert resolve(_row(RESOLUTION_WIRE), "sweep_tick", ctx).close is False

    def test_the_startup_cell_needs_no_fault_liveness_guard(self):
        """Deliberate asymmetry: a restart re-derives every wire fact from scratch, and
        a printer already positive has answered the question (over a RUNNING print, once it printed past
        the fault — R27)."""
        ctx = _ctx_of(_state("RUNNING", hms=[_ptfe_breakage_hms()], tray_now=1))

        verdict = resolve(_row(RESOLUTION_WIRE), "startup", ctx)

        assert (verdict.close, verdict.source) == (True, RESOLVE_REARM)

    @pytest.mark.parametrize("live", ["IDLE", "FAILED"])
    def test_a_restart_the_driver_owes_an_unload_stands_at_startup(self, live):
        """A deploy mid-restart (operator ruling 2026-09-29): the driver STOPPED this row's
        job to restart it and the row is still open (``restart_owed`` — the stop on the step
        ledger, read by the rearm, whatever the unload answered). The printer reads IDLE /
        FAILED because of that stop — not because the hold is over — so the cell stands and
        the continuation re-enters to finish (the unload, or the park and the close), instead
        of handing the dispatcher a tube that may still hold the stalled filament. Only the
        startup occasion reads it; without it the same reading closes as before."""
        owed = Context(state=_state(live), driver_live=False, restart_owed=True)
        verdict = resolve(_row(RESOLUTION_WIRE, status=STATUS_RECOVERING), "startup", owed)
        assert verdict.close is False
        assert "restart continuation re-enters" in verdict.evidence

        not_owed = replace(owed, restart_owed=False)
        assert resolve(_row(RESOLUTION_WIRE, status=STATUS_RECOVERING), "startup", not_owed).close is True


def _runout_same_slot_hms() -> HMSError:
    """``0700_8011`` — the slot-agnostic runout that opens a runout row."""
    return HMSError(code="8011", attr=0x07000000, module=7, severity=2, full_code="0700000000008011")


def _wire_row(kind: str = KIND_RUNOUT, *, fault_layer: int | None = 50, job_id: str = _JOB) -> PrinterIncident:
    """An open WIRE-class row the AMS entry opened at job layer ``fault_layer``, its ``codes`` the entry's
    own fingerprint of the runout (or, for a jam, the feed fault) it opened on."""
    opened_on = [_runout_same_slot_hms()] if kind == KIND_RUNOUT else [_jam_hms()]
    return PrinterIncident(
        id=1,
        printer_id=7,
        job_id=job_id,
        item_id=None,
        kind=kind,
        code="0700_8011" if kind == KIND_RUNOUT else "0700_8010",
        codes=candidate_fingerprint(live_candidates(_state(hms=opened_on))),
        slot_global_tray=2,
        status=STATUS_ESCALATED,
        created_at=_OPENED_AT,
        fault_layer=fault_layer,
    )


class TestTheWireCloseWaitsForTheLayerAdvance:
    """R27 (2026-10-12). Production closed a wire-class AMS row over RUNNING only after the sweep's 120 s
    dwell; the rewrite deleted the dwell, so a fault flapping around the RUNNING edge closed its row,
    re-opened as a NEW row and was owed a fresh farm resume (the 574 reopen loop). The event that replaces
    the dwell: over a RUNNING print the row closes only once the job printed a layer FED past its
    ``fault_layer`` — or at the job's terminal. A flap, a re-pause or a firmware change never advances one."""

    @staticmethod
    def _running(*, layer: int, job: str = _JOB, hms=None, live: str = "RUNNING", feed=None) -> Context:
        state = _state(live, hms=hms, tray_now=1, layer=layer)
        state.subtask_id = job
        return _ctx_of(state) if feed is None else replace(_ctx_of(state), feed=feed)

    @pytest.mark.parametrize("kind", [KIND_RUNOUT, KIND_JAM])
    @pytest.mark.parametrize("occasion", ["running_edge", "sweep_tick", "startup"])
    def test_the_print_at_the_faults_layer_keeps_it_and_the_next_layer_closes_it(self, kind, occasion):
        row = _wire_row(kind, fault_layer=50)

        held = resolve(row, occasion, self._running(layer=50))
        assert held.close is False
        assert "has not printed a layer fed past the fault" in held.evidence

        assert resolve(row, occasion, self._running(layer=51)).close is True

    def test_slow_firmware_the_advance_comes_late_and_still_closes(self):
        """However long the printer sits RUNNING on the fault's layer (a slow first layer after the resume,
        a long layer), nothing closes the row on a window — and the advance, whenever it lands, does."""
        row = _wire_row(fault_layer=50)
        for _ in range(20):
            assert resolve(row, "sweep_tick", self._running(layer=50)).close is False

        verdict = resolve(row, "sweep_tick", self._running(layer=51))

        assert (verdict.close, verdict.source) == (True, RESOLVE_WIRE_CLEAR)

    def test_a_layer_advanced_on_air_is_not_printed_past_the_fault(self):
        """FED: a layer the feed state measured printed with nothing fed (``first_unfed_layer``) is the air
        lane's, never the print running on past the fault."""
        row = _wire_row(fault_layer=5)

        assert resolve(row, "sweep_tick", self._running(layer=12, feed=_air_feed())).close is False

    def test_the_jobs_terminal_still_closes_it_at_the_faults_layer(self):
        """The other event: a job hold cannot outlive its job."""
        ctx = replace(
            self._running(layer=50, live="FAILED"), terminal=TerminalEvent(status="failed", eject=False, job_id=_JOB)
        )

        verdict = resolve(_wire_row(fault_layer=50), "job_terminal", ctx)

        assert (verdict.close, verdict.source) == (True, RESOLVE_TERMINAL)

    @pytest.mark.parametrize("live", ["IDLE", "FINISH", "FAILED"])
    def test_a_printer_reporting_the_job_over_is_not_over_running(self, live):
        """The rule is for a close over a RUNNING print: a printer reporting the job over or idle closes it on
        a quiet wire, as before (the terminal said so too)."""
        assert resolve(_wire_row(fault_layer=50), "sweep_tick", self._running(layer=50, live=live)).close is True

    @pytest.mark.parametrize("layer", [0, 1])
    def test_another_job_running_closes_it(self, layer):
        """The interrupted job is over and another one runs — the statement its terminal makes too, so it is
        not a RUNNING of the job the row holds, whatever its layer."""
        row = _wire_row(fault_layer=120)

        assert resolve(row, "sweep_tick", self._running(layer=layer, job="other-job")).close is True

    def test_an_id_less_job_is_read_against_the_fault_layer(self):
        """A job naming no id (``job_identity`` ``unknown``) is compared like the row's own: a layer past the
        fault is past it, the fault's own layer is not."""
        row = _wire_row(fault_layer=50, job_id="")

        assert resolve(row, "sweep_tick", self._running(layer=50, job="")).close is False
        assert resolve(row, "sweep_tick", self._running(layer=51, job="")).close is True

    def test_a_row_with_no_recorded_fault_layer_closes_as_production_did(self):
        """A row opened before the column (or with no client to read the layer) cannot measure the advance
        on its own job: it closes over RUNNING as production did, with no dwell. Every row the AMS entry
        opens now records one."""
        row = _wire_row(fault_layer=None)

        verdict = resolve(row, "running_edge", self._running(layer=50))

        assert (verdict.close, verdict.source) == (True, RESOLVE_OBSERVED_RUNNING)

    def test_a_power_loss_row_is_not_an_ams_row(self):
        """The power-loss hold is wire-class but not an AMS fault: its farm resume's RUNNING ends it at once
        (``pause_recovery``), at whatever layer — R27 does not reach it."""
        row = _replace_row(_wire_row(fault_layer=50), kind=KIND_POWER_LOSS, code="0300_8007", codes="")

        assert resolve(row, "running_edge", self._running(layer=0)).close is True

    def test_a_live_driver_still_owns_the_edge(self):
        verdict = resolve(_wire_row(fault_layer=50), "running_edge", replace(self._running(layer=51), driver_live=True))

        assert verdict.close is False
        assert "closer stands aside" in verdict.evidence

    # --- the new-fault occasion: the flap stays absorbed, a different fault is its own incident ---------

    def test_the_rows_own_fault_standing_again_is_the_flap_it_stays_open_for(self):
        """The 574 shape: the runout flaps back on at the fault's layer, or on a later one — the row's own
        fault is the hold it holds; it is never closed to make room for itself."""
        row = _wire_row(fault_layer=50)
        for layer in (50, 51):
            verdict = resolve(row, "new_fault", self._running(layer=layer, hms=[_runout_same_slot_hms()]))
            assert verdict.close is False
            assert "own fault still stands" in verdict.evidence

    def test_a_different_fault_before_the_print_ran_arrived_inside_the_hold(self):
        """A jam raised while the operator refills the runout slot, the printer never resumed: the hold
        stands, and the entry's outrank test re-classifies it if the jam is worse."""
        verdict = resolve(
            _wire_row(fault_layer=50), "new_fault", self._running(layer=50, live="PAUSE", hms=[_jam_hms()])
        )

        assert verdict.close is False
        assert "inside the hold" in verdict.evidence

    def test_a_different_fault_after_the_layer_advance_is_its_own_incident(self):
        verdict = resolve(_wire_row(fault_layer=50), "new_fault", self._running(layer=51, hms=[_jam_hms()]))

        assert (verdict.close, verdict.source) == (True, RESOLVE_OBSERVED_RUNNING)
        assert verdict.evidence == incident_resolution._WIRE_EVIDENCE_NEW_FAULT_PAST  # noqa: SLF001

    def test_a_different_fault_after_the_job_was_seen_running_is_its_own_incident(self):
        """A person's touchscreen resume (no farm resume on the row): the RUNNING edge recorded the row's own job
        running since the row opened (``Context.job_ran``), so a jam inside the fault's layer is its own
        incident — while the row's own fault flapping back stays held."""
        row = _wire_row(fault_layer=50)

        jam = resolve(row, "new_fault", replace(self._running(layer=50, live="PAUSE", hms=[_jam_hms()]), job_ran=True))
        flap = resolve(
            row,
            "new_fault",
            replace(self._running(layer=50, live="PAUSE", hms=[_runout_same_slot_hms()]), job_ran=True),
        )

        assert (jam.close, jam.source) == (True, RESOLVE_OBSERVED_RUNNING)
        assert jam.evidence == incident_resolution._WIRE_EVIDENCE_NEW_FAULT_RESUMED  # noqa: SLF001
        assert flap.close is False

    def test_the_sighting_is_the_rows_own_job_running_with_no_driver(self):
        """What the RUNNING edge records (``own_job_running``): the row's job (or an id-less one) RUNNING, no
        driver live (a driver's lever resume is its procedure), no eject."""
        row = _wire_row(fault_layer=50)

        assert incident_resolution.own_job_running(row, self._running(layer=50)) is True
        assert incident_resolution.own_job_running(row, replace(self._running(layer=50), driver_live=True)) is False
        assert incident_resolution.own_job_running(row, self._running(layer=50, job="other-job")) is False
        assert incident_resolution.own_job_running(row, self._running(layer=50, live="PAUSE")) is False

    def test_a_different_fault_after_the_farms_resume_reached_is_its_own_incident(self):
        """Inside the fault's own layer, the farm's resume on the row REACHED the printer (the record): the
        print ran after the fault — production closed the row right there — so a jam now is not this hold."""
        printer_incidents._open_cache[7] = {  # noqa: SLF001
            1: {
                "id": 1,
                "kind": KIND_RUNOUT,
                "farm_actions": [
                    {
                        "action_id": 50,
                        "command": "resume",
                        "target": None,
                        "answer": "reached",
                        "sent_at": "2026-10-12T08:00:00",
                        "answered_at": "2026-10-12T08:00:05",
                        "step_reading": None,
                        "reason": None,
                        "unsent": False,
                    }
                ],
            }
        }
        try:
            verdict = resolve(_wire_row(fault_layer=50), "new_fault", self._running(layer=50, hms=[_jam_hms()]))
        finally:
            printer_incidents._reset_state()  # noqa: SLF001

        assert (verdict.close, verdict.source) == (True, RESOLVE_OBSERVED_RUNNING)
        assert verdict.evidence == incident_resolution._WIRE_EVIDENCE_NEW_FAULT_RESUMED  # noqa: SLF001


def _replace_row(row: PrinterIncident, **changes) -> PrinterIncident:
    """A copy of an unattached row with ``changes`` applied."""
    fields = {
        name: getattr(row, name)
        for name in (
            "id",
            "printer_id",
            "job_id",
            "item_id",
            "kind",
            "code",
            "codes",
            "slot_global_tray",
            "status",
            "created_at",
            "fault_layer",
        )
    }
    return PrinterIncident(**{**fields, **changes})


class TestTheRepairLaneMotionEvidence:
    def _ctx(self, *, live="IDLE", hms=None, load_at=None, ams_status_main=0, tray_now=None, layer=10):
        state = _state(live, hms=hms, ams_status_main=ams_status_main, tray_now=tray_now, layer=layer)
        return _ctx_of(state, observed_load_at=load_at)

    def test_an_idle_clean_printer_is_not_evidence(self):
        """THE 003-H2S pin: the firmware wipes its standing HMS list at every terminal,
        so a printer with filament still jammed in its PTFE path reads exactly like a
        repaired one. Silence is not evidence; motion is."""
        assert resolve(_row(RESOLUTION_REPAIR), "sweep_tick", self._ctx()).close is False

    def test_a_completed_load_after_the_fault_closes_it(self):
        ctx = self._ctx(load_at=_OPENED_AT + timedelta(seconds=30))

        verdict = resolve(_row(RESOLUTION_REPAIR), "sweep_tick", ctx)

        assert (verdict.close, verdict.source) == (True, RESOLVE_REPAIR_OBSERVED)
        assert verdict.evidence == incident_resolution._REPAIR_EVIDENCE_LOAD  # noqa: SLF001

    def test_a_load_before_the_fault_is_the_one_that_jammed(self):
        ctx = self._ctx(load_at=_OPENED_AT - timedelta(seconds=30))

        assert resolve(_row(RESOLUTION_REPAIR), "sweep_tick", ctx).close is False

    def test_a_print_fed_past_the_fault_layer_closes_it(self):
        """A print FEEDING through the path (RUNNING with the active extruder fed — K12) that has
        printed a layer past the one the fault stopped it at: the layer advance is the event."""
        verdict = resolve(_row(RESOLUTION_REPAIR), "sweep_tick", self._ctx(live="RUNNING", tray_now=1))

        assert verdict.source == RESOLVE_REPAIR_OBSERVED
        assert verdict.evidence == incident_resolution._REPAIR_EVIDENCE_RUNNING  # noqa: SLF001

    def test_a_print_feeding_at_the_fault_layer_has_not_run_past_it(self):
        """Fed and RUNNING, but still at the fault's own layer: nothing has run through the path since
        the fault yet. The next layer answers — no clock."""
        row = _row(RESOLUTION_REPAIR, fault_layer=10)

        held = resolve(row, "sweep_tick", self._ctx(live="RUNNING", tray_now=1, layer=10))
        assert held.close is False
        assert "past the fault" in held.evidence
        assert resolve(row, "sweep_tick", self._ctx(live="RUNNING", tray_now=1, layer=11)).close is True

    def test_a_row_with_no_fault_layer_needs_other_evidence(self):
        """A row opened before the column existed records no layer: a feeding print on its job says
        nothing about "after the fault"; the record's uncommanded load still does."""
        row = _row(RESOLUTION_REPAIR, fault_layer=None)

        assert resolve(row, "sweep_tick", self._ctx(live="RUNNING", tray_now=1, layer=40)).close is False
        loaded = self._ctx(load_at=_OPENED_AT + timedelta(seconds=30))
        assert resolve(row, "sweep_tick", loaded).close is True

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

        assert resolve(_row(RESOLUTION_REPAIR), "sweep_tick", self._ctx(live="RUNNING", tray_now=1)).close is False

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

    def test_the_startup_cell_takes_the_same_evidence(self):
        ctx = self._ctx(live="RUNNING", tray_now=1)

        verdict = resolve(_row(RESOLUTION_REPAIR), "startup", ctx)

        assert (verdict.close, verdict.source) == (True, RESOLVE_REARM)

    def test_recover_closes_it_and_a_routine_clear_plate_does_not(self):
        """The two plate verbs are different statements. "The bed is empty" says
        nothing about the filament path; "I inspected this machine" is the class's
        third return-to-normal."""
        base = Context(state=_state(), driver_live=False)

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

    def _ctx(self, *, status="completed", eject=False, job_id=_JOB, last_layer=20, hms=None, unfed=False, load_at=None):
        return Context(
            state=_state("FINISH", hms=hms),
            driver_live=False,
            observed_load_at=load_at,
            terminal=TerminalEvent(
                status=status, eject=eject, job_id=job_id, printed_unfed=unfed, last_layer=last_layer
            ),
        )

    def test_the_same_job_completing_closes_it(self):
        verdict = resolve(_row(RESOLUTION_REPAIR), "job_terminal", self._ctx())

        assert (verdict.close, verdict.source) == (True, RESOLVE_REPAIR_COMPLETED)
        assert verdict.evidence == incident_resolution._REPAIR_EVIDENCE_COMPLETED  # noqa: SLF001

    def test_a_finish_that_printed_on_air_is_not_filament_fed_to_the_end(self):
        """011-H2S and 014-H2S, 2026-10-09: resumed onto an empty toolhead, the jobs printed the
        rest of the plate on AIR and the printer still said FINISH. The terminal payload measured
        it (``JobExtent.printed_unfed`` — a layer printed with nothing fed, below the last), so the
        completion proves nothing about the path, and the physical hold stands."""
        verdict = resolve(_row(RESOLUTION_REPAIR), "job_terminal", self._ctx(unfed=True))

        assert verdict.close is False
        assert "printed on air" in verdict.evidence

    def test_the_completion_callback_hands_the_closer_the_measured_air_finish(self):
        """``main.on_print_complete`` builds the closer's ``TerminalEvent`` with the payload's own
        ``printed_unfed`` — read off the SAME parse (``_evidence``, the ``DepositEvidence`` the
        outcome was built from), never a second reading of the payload. A SOURCE pin: the
        callback has no light harness, and a construction that silently drops the field passes
        every behaviour test of this table."""
        import ast
        import inspect

        from backend.app import main

        tree = ast.parse(inspect.getsource(main.on_print_complete).lstrip())
        (call,) = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "TerminalEvent"
        ]
        keywords = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}

        assert keywords.get("printed_unfed") == "_evidence.printed_unfed"
        # ...and how far the job got, off the same parse: the completed arm's "printed past the fault".
        assert keywords.get("last_layer") == "_evidence.last_layer_num"

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

    @pytest.mark.parametrize(("row_job", "echo"), [("0", "0"), ("", "0"), ("0", ""), (_JOB, "0")])
    def test_a_lan_print_id_names_no_job_either(self, row_job, echo):
        """``"0"`` is the printer's "no job" as much as ``""`` is (``job_identity``): repair
        evidence needs the terminal to NAME the interrupted job, so two ``"0"``s — a hand
        ``==`` once called them the same job — launder nothing."""
        row = _row(RESOLUTION_REPAIR)
        row.job_id = row_job

        assert resolve(row, "job_terminal", self._ctx(job_id=echo)).close is False

    @pytest.mark.parametrize("status", ["aborted", "failed", "cancelled", "FINISH", ""])
    def test_only_completed_counts(self, status):
        """Shape 38's pin, unchanged: a stop / failure / abort is very often the
        operator ending the very print the fault broke."""
        assert resolve(_row(RESOLUTION_REPAIR), "job_terminal", self._ctx(status=status)).close is False

    def test_a_completed_eject_sweep_is_not_repair_evidence(self):
        """The sweep is filament-less, and it completes on every production cycle — so
        without this the farm's own eject would launder every physical hold it met."""
        assert resolve(_row(RESOLUTION_REPAIR), "job_terminal", self._ctx(eject=True)).close is False

    def test_no_layer_printed_past_the_fault_stands(self):
        """The disqualifier for a pull-back timeout raised at END-OF-PRINT unload: the job completes,
        but its last layer IS the fault's — nothing ran through the path after the fault."""
        ctx = self._ctx(last_layer=_FAULT_LAYER)

        verdict = resolve(_row(RESOLUTION_REPAIR), "job_terminal", ctx)

        assert verdict.close is False
        assert "no layer past the fault" in verdict.evidence

    def test_a_load_nobody_commanded_after_the_fault_is_evidence_too(self):
        """The record's uncommanded load after the row opened is filament through the path."""
        ctx = self._ctx(last_layer=_FAULT_LAYER, load_at=_OPENED_AT + timedelta(seconds=30))

        assert resolve(_row(RESOLUTION_REPAIR), "job_terminal", ctx).close is True

    def test_a_row_with_no_fault_layer_stands_on_the_completion_alone(self):
        """A row opened before the column existed: the completion cannot say it ran past the fault."""
        assert resolve(_row(RESOLUTION_REPAIR, fault_layer=None), "job_terminal", self._ctx()).close is False

    def test_a_fault_still_standing_at_the_terminal_stands(self):
        """Evidence is not a latch here either."""
        ctx = self._ctx(hms=[_ptfe_breakage_hms()])

        assert resolve(_row(RESOLUTION_REPAIR), "job_terminal", ctx).close is False

    def test_a_wire_row_still_closes_on_any_terminal(self):
        """The completed arm is the REPAIR class's. A wire hold is over when its job
        ends, whatever the outcome — that rule is untouched."""
        verdict = resolve(_row(RESOLUTION_WIRE), "job_terminal", self._ctx(status="aborted", last_layer=0))

        assert (verdict.close, verdict.source) == (True, RESOLVE_TERMINAL)


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


def _physical_row(
    opened_on: list[HMSError], *, created_at: datetime, fault_layer: int | None = _FAULT_LAYER
) -> PrinterIncident:
    """An open AMS-side PHYSICAL row (the ``repair`` class) whose ``codes`` are the entry
    gate's own fingerprint of the faults it opened on, at job layer ``fault_layer``."""
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
        fault_layer=fault_layer,
    )


def _opened_minutes_ago() -> datetime:
    """``created_at`` safely in the past."""
    return datetime.utcnow() - timedelta(minutes=5)


class TestTheNewFaultOccasion:
    """The AMS entry gate asks the open AMS row whether the fault now on the wire ENDS it.

    002-H2S 2026-09-15/16: three ``0700_8010`` jams swallowed behind ``0700_0011`` +
    ``0700_8004`` pull-back rows; 006-H2S 2026-09-21: a ``0300_801E`` behind incident 289.
    The rework made the RUNNING edge no repair evidence for the class, so the rows
    outlived the resume that had repaired the path, and the entry refused every new fault
    as "already held". A repair row now closes ``repair_observed`` on a new fault once the
    print demonstrably RAN through the path after it opened — a layer printed FED past the row's
    ``fault_layer`` (the job's own record), or a load nobody commanded since — and on nothing weaker.
    """

    @staticmethod
    def _now_on_wire(hms, *, live="PAUSE", driver_live=False, layer=10, job=_JOB, feed=None) -> Context:
        state = _state(live, hms=hms, tray_now=1, layer=layer)
        state.subtask_id = job
        return Context(
            state=state,
            driver_live=driver_live,
            feed=feed if feed is not None else _feed_of(state),
            job_layer=layer,
        )

    @pytest.mark.parametrize(
        "new_fault",
        [pytest.param(_jam_hms, id="002-H2S-0700_8010"), pytest.param(_extruder_overload_hms, id="006-H2S-0300_801E")],
    )
    def test_a_new_fault_after_a_print_ran_through_the_path_closes_it(self, new_fault):
        """The operator repaired the path and resumed; it printed on, past the fault's layer."""
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())

        verdict = resolve(row, "new_fault", self._now_on_wire([new_fault()], live="RUNNING"))

        assert (verdict.close, verdict.source) == (True, RESOLVE_REPAIR_OBSERVED)
        assert verdict.evidence == incident_resolution._REPAIR_EVIDENCE_NEW_FAULT  # noqa: SLF001

    def test_the_rows_own_fault_still_standing_keeps_it(self):
        """A new code BESIDE the row's own is the same blockage, better described — the
        entry's outrank test is what re-classifies a row, never this close."""
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())

        verdict = resolve(row, "new_fault", self._now_on_wire([*_pull_back_hms(), _jam_hms()]))

        assert verdict.close is False
        assert "own fault still stands" in verdict.evidence

    @pytest.mark.parametrize("lane", [0, 1], ids=["hms-lane-0700_0011", "print-error-lane-0700_8004"])
    def test_one_own_code_on_either_wire_lane_keeps_it(self, lane):
        """Both lanes are the row's own words: either one standing is the old fault."""
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())

        verdict = resolve(row, "new_fault", self._now_on_wire([_pull_back_hms()[lane], _jam_hms()]))

        assert verdict.close is False

    def test_the_same_code_on_another_slot_is_a_different_fault(self):
        """The comparison is the entry gate's own slot-qualified token: a second slot's
        breakage is a new fault, exactly as a second roll running dry is a new runout."""
        row = _physical_row([_ptfe_breakage_hms(tray_id=3)], created_at=_opened_minutes_ago())

        verdict = resolve(row, "new_fault", self._now_on_wire([_ptfe_breakage_hms(tray_id=1)]))

        assert (verdict.close, verdict.source) == (True, RESOLVE_REPAIR_OBSERVED)

    def test_no_actionable_fault_is_no_new_fault(self):
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())

        verdict = resolve(row, "new_fault", self._now_on_wire([]))

        assert verdict.close is False

    def test_layers_before_a_re_classification_are_not_after_the_fault(self):
        """An UPGRADED row keeps the jam's ``created_at`` — a recovery driver's own lever resumes could
        print layers after it — but the upgrade re-stamps ``fault_layer`` to the worse fault's layer, so
        those layers are not "past the fault"."""
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago(), fault_layer=10)

        verdict = resolve(row, "new_fault", self._now_on_wire([_jam_hms()], layer=10))

        assert verdict.close is False
        assert "no print ran through the path" in verdict.evidence

    def test_a_job_that_printed_air_proves_nothing_about_the_path(self):
        """K12: layers printed with NOTHING fed (the feed state's ``first_unfed_layer``) ran no filament
        through anything — the next jam is still the same blockage as far as the farm can tell."""
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())

        verdict = resolve(row, "new_fault", self._now_on_wire([_jam_hms()], layer=12, feed=_air_feed()))

        assert verdict.close is False

    def test_another_job_that_printed_is_evidence(self):
        """The interrupted job is over and another one printed through the path since."""
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())

        verdict = resolve(row, "new_fault", self._now_on_wire([_jam_hms()], job="other-job", layer=1))

        assert verdict.close is True

    def test_a_job_naming_no_id_proves_nothing(self):
        """``job_identity``: an id-less job is ``unknown`` — never the same job, never another."""
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())

        assert resolve(row, "new_fault", self._now_on_wire([_jam_hms()], job="")).close is False

    def test_a_live_driver_at_the_verdict_stands_aside(self):
        """The told-occasion ownership rule: closing a row from under a live driver would let
        the entry spawn a second driver onto one AMS (006-H2S 2026-09-04)."""
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())

        verdict = resolve(row, "new_fault", self._now_on_wire([_jam_hms()], driver_live=True))

        assert verdict.close is False
        assert "closer stands aside" in verdict.evidence

    def test_the_evidence_is_the_same_after_a_restart(self):
        """The motion ledger was process memory: a restart emptied it and the row stood until the new
        process happened to see the path run. The job's own record — its layer past the row's
        ``fault_layer`` — reads the same in any process."""
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())

        assert resolve(row, "new_fault", self._now_on_wire([_jam_hms()])).close is True


class TestTheCompletedArmReadsTheJobsOwnRecord:
    """``_repair_job_terminal`` reads the terminal PAYLOAD'S peak against the row's ``fault_layer`` —
    durable both — never a sighting held in memory."""

    @staticmethod
    def _completed(row: PrinterIncident, *, last_layer: int):
        return resolve(
            row,
            "job_terminal",
            Context(
                state=_state("FINISH"),
                driver_live=False,
                terminal=TerminalEvent(status="completed", eject=False, job_id=_JOB, last_layer=last_layer),
            ),
        )

    def test_a_completion_past_the_fault_closes_it(self):
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())

        verdict = self._completed(row, last_layer=40)

        assert (verdict.close, verdict.source) == (True, RESOLVE_REPAIR_COMPLETED)

    def test_a_re_classified_row_reads_its_re_stamped_layer(self):
        """A driver's own resumes before an upgrade printed up to the worse fault — not past it."""
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago(), fault_layer=40)

        assert self._completed(row, last_layer=40).close is False


class TestRepairEvidenceNeedsFilamentFed:
    """K12 / C3b (011-H2S 2026-10-09, 014-H2S 2026-10-09/10): a print resumed onto an EMPTY
    toolhead read RUNNING, and RUNNING was repair evidence — "a print ran through the path" —
    so a physical hold could close on a print laying down air. The ``repair`` class's motion
    evidence is now ``feeding_through_path``: RUNNING with no eject AND the toolhead's feed state
    FED or TAIL from an AMS feeder (``feed_state``, invariant 16 — the ACTIVE extruder's phase). It
    decides the (b) cell, and the new-fault arm reads the same feed state (a job that printed air, or
    runs with nothing at the active extruder, ran nothing through the path)."""

    @staticmethod
    def _ctx(state: PrinterState, **kw) -> Context:
        return _ctx_of(state, **kw)

    @pytest.mark.parametrize("occasion", ["sweep_tick", "startup"])
    def test_a_print_running_on_air_never_closes_a_physical_hold(self, occasion):
        verdict = resolve(_row(RESOLUTION_REPAIR), occasion, self._ctx(_state("RUNNING", tray_now=255)))

        assert verdict.close is False
        assert "no AMS feeder" in verdict.evidence

    @pytest.mark.parametrize("occasion", ["sweep_tick", "startup"])
    def test_a_print_off_the_external_spool_is_not_the_ams_path(self, occasion):
        """The class is AMS-side physical holds only (``RESOLVES_ON[(physical, False)]``). The
        external holder bypasses the AMS path entirely, so a print fed from it says nothing
        about the path the hold is for — unlike the toolhead class, whose question is only
        "is it printing air"."""
        verdict = resolve(_row(RESOLUTION_REPAIR), occasion, self._ctx(_state("RUNNING", tray_now=254)))

        assert verdict.close is False

    @pytest.mark.parametrize("occasion", ["sweep_tick", "startup"])
    def test_a_fed_running_print_still_closes_it(self, occasion):
        verdict = resolve(_row(RESOLUTION_REPAIR), occasion, self._ctx(_state("RUNNING", tray_now=1)))

        assert verdict.close is True
        assert verdict.evidence == incident_resolution._REPAIR_EVIDENCE_RUNNING  # noqa: SLF001

    def test_an_air_print_never_lets_a_new_fault_close_a_physical_row(self):
        """The new-fault arm reads the feed state: a print RUNNING with nothing at the active extruder
        ran no filament through the path, so the next jam is still the same blockage."""
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())

        verdict = resolve(row, "new_fault", self._ctx(_state("RUNNING", hms=[_jam_hms()], tray_now=255)))

        assert verdict.close is False
        assert "no print ran through the path" in verdict.evidence

    def test_an_air_print_never_lets_its_completion_close_a_physical_row(self):
        """The completed arm reads the payload's own measurement of the air (``printed_unfed``) —
        and a job whose air run never advanced a layer printed nothing past the fault either."""
        row = _physical_row(_pull_back_hms(), created_at=_opened_minutes_ago())
        unfed = TerminalEvent(status="completed", eject=False, job_id=_JOB, printed_unfed=True, last_layer=40)
        still = TerminalEvent(status="completed", eject=False, job_id=_JOB, last_layer=_FAULT_LAYER)

        assert resolve(row, "job_terminal", self._ctx(_state("FINISH"), terminal=unfed)).close is False
        assert resolve(row, "job_terminal", self._ctx(_state("FINISH"), terminal=still)).close is False

    @pytest.mark.parametrize(
        ("snow", "active", "feeding"),
        [
            pytest.param({0: 255, 1: 3}, 0, False, id="active_empty_beside_a_fed_deputy"),
            pytest.param({0: 255, 1: 3}, 1, True, id="active_fed_beside_an_empty_deputy"),
        ],
    )
    def test_on_a_dual_nozzle_the_active_extruder_decides(self, snow, active, feeding):
        """H2C/H2D: an empty ACTIVE nozzle beside a loaded one prints air (K1) — the deputy's
        filament is not feeding the print."""
        running = _state("RUNNING", tray_now=3, snow=snow, active=active)

        assert resolve(_row(RESOLUTION_REPAIR), "sweep_tick", self._ctx(running)).close is feeding

    @pytest.mark.parametrize("occasion", ["running_edge", "sweep_tick", "startup"])
    def test_a_job_pause_still_closes_on_running_whatever_the_feed(self, occasion):
        """``running_without_eject`` keeps serving the ``job_pause`` class: the plate check's
        answer is the paused job printing again, and its resume must not require filament —
        a refill is the toolhead lanes' business, never the plate check's."""
        verdict = resolve(_row(RESOLUTION_JOB_PAUSE), occasion, self._ctx(_state("RUNNING", tray_now=255)))

        assert (verdict.close, verdict.source) == (
            True,
            RESOLVE_REARM if occasion == "startup" else RESOLVE_OBSERVED_RUNNING,
        )


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

    @pytest.mark.parametrize(
        ("status", "live", "expected"),
        [
            # A job-pause episode's ``recovering`` is a promise only a LIVE driver keeps: with
            # none, the row is adjudicable (else a job that ended unseen strands it forever).
            (STATUS_RECOVERING, False, False),
            (STATUS_RECOVERING, True, True),
            (STATUS_ESCALATED, True, True),
            (STATUS_ESCALATED, False, False),
        ],
    )
    def test_a_job_pause_row_is_owned_only_by_a_live_driver(self, status, live, expected):
        assert driver_owns(_row(RESOLUTION_JOB_PAUSE, status=status), live=live) is expected

    @pytest.mark.parametrize(
        "resolution", [RESOLUTION_WIRE, RESOLUTION_REPAIR, RESOLUTION_OPERATOR, RESOLUTION_DECLARED]
    )
    def test_every_other_class_keeps_the_promise_reading(self, resolution):
        """An AMS ``recovering`` row with no live task is the startup re-entry's to drive."""
        assert driver_owns(_row(resolution, status=STATUS_RECOVERING), live=False) is True


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


class TestTheToolheadLane:
    """K10 (2026-10-10): a ``toolhead_refill`` row — the farm refilling an EMPTY toolhead on a
    printer no AMS row holds — returns to normal on a print running FED (the toolhead's feed state
    FED or TAIL — ``Context.feed``, invariant 16), on its job's end, or on Recover. A RUNNING print
    with nothing fed is the hold itself (a screen resume onto air), never its end."""

    def _ctx(self, live="RUNNING", *, tray_now=0, driver_live=False, **kw):
        state = _state(live, tray_now=tray_now)
        return Context(state=state, driver_live=driver_live, feed=_feed_of(state), **kw)

    def test_a_print_running_on_air_is_not_its_end(self):
        verdict = resolve(_row(RESOLUTION_TOOLHEAD), "running_edge", self._ctx(tray_now=255))
        assert verdict.close is False and "nothing fed" in verdict.evidence

    def test_the_refill_driver_owns_its_own_resume(self):
        """A RUNNING edge the refill driver's own resume produced is its reading (the wire cell's
        rule): it stands while a driver is live, and the driver closes its own row."""
        verdict = resolve(_row(RESOLUTION_TOOLHEAD), "running_edge", self._ctx(driver_live=True))
        assert verdict.close is False and "driver is live" in verdict.evidence

    def test_an_eject_sweep_is_not_a_fed_print(self):
        """A sweep is filament-less and is not the job the hold is about."""
        row = _row(RESOLUTION_TOOLHEAD)
        assert resolve(row, "sweep_tick", self._ctx(tray_now=0)).close is True
        plate_occupancy.hydrate_eject(7, PendingEject(purpose="production", run_id=None, queue_item_id=1))
        assert resolve(row, "sweep_tick", self._ctx(tray_now=0)).close is False

    @pytest.mark.parametrize("live", ["FINISH", "FAILED", "IDLE"])
    def test_the_sweep_closes_a_hold_whose_job_the_printer_reports_over(self, live):
        """A toolhead hold cannot outlive its job: the printer's POSITIVE report that the job is
        over (a terminal the farm never saw) closes it."""
        verdict = resolve(_row(RESOLUTION_TOOLHEAD), "sweep_tick", self._ctx(live, tray_now=255))
        assert (verdict.close, verdict.source) == (True, RESOLVE_JOB_ENDED_UNSEEN)

    def test_a_paused_print_is_the_hold(self):
        for occasion in ("sweep_tick", "startup"):
            verdict = resolve(_row(RESOLUTION_TOOLHEAD), occasion, self._ctx("PAUSE", tray_now=255))
            assert verdict.close is False, occasion

    def test_startup_closes_a_job_the_printer_reports_over(self):
        verdict = resolve(_row(RESOLUTION_TOOLHEAD), "startup", self._ctx("IDLE", tray_now=255))
        assert (verdict.close, verdict.source) == (True, RESOLVE_REARM)

    def test_a_routine_plate_clear_is_not_a_statement_about_the_toolhead(self):
        verdict = resolve(_row(RESOLUTION_TOOLHEAD), "plate_cleared", self._ctx(cleared=ClearedEvent(recover=False)))
        assert verdict.close is False

    def test_the_job_terminal_stands_while_the_driver_lives(self):
        terminal = TerminalEvent(status="failed", eject=False, job_id=_JOB)
        assert (
            resolve(_row(RESOLUTION_TOOLHEAD), "job_terminal", self._ctx(driver_live=True, terminal=terminal)).close
            is False
        )
        assert resolve(_row(RESOLUTION_TOOLHEAD), "job_terminal", self._ctx(terminal=terminal)).close is True


class TestTheResumeBudget:
    """``incident_resolution.resume_spent`` — THE one decider of how many farm resumes a row may send (R14,
    2026-10-12). A RUNOUT row's refill resume that the printer REFUSED, or left unanswered past its self-heal
    bound, is owed again until 3 resumes went out (measured on production: 56 refill episodes over 90 days,
    the farm published 2 resumes, both landed in 7-11 s, no refusal ever logged — the cap only bounds a
    loop); a REACHED one spends the row; every other class stays at one resume per row."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        printer_incidents._reset_state()  # noqa: SLF001
        yield
        printer_incidents._reset_state()  # noqa: SLF001

    @staticmethod
    def _row_with(kind: str, *answers: str | None, unsent: tuple[int, ...] = ()) -> None:
        actions = [
            {
                "action_id": 50 + index,
                "command": "resume",
                "target": None,
                "answer": answer,
                "sent_at": "2026-10-12T08:00:00",
                "answered_at": None if answer is None else "2026-10-12T08:00:05",
                "step_reading": None,
                "reason": None,
                "unsent": index in unsent,
            }
            for index, answer in enumerate(answers)
        ]
        printer_incidents._open_cache[7] = {1: {"id": 1, "kind": kind, "farm_actions": actions}}  # noqa: SLF001

    def test_the_cap_is_three(self):
        assert incident_resolution.RUNOUT_RESUME_CAP == 3

    def test_no_resume_yet_is_owed(self):
        self._row_with(KIND_RUNOUT)
        assert incident_resolution.resume_spent(7, 1) is None

    def test_a_runout_resume_out_and_awaiting_the_printer_spends_it_without_a_give_up(self):
        self._row_with(KIND_RUNOUT, None)
        spent = incident_resolution.resume_spent(7, 1)
        assert spent is not None and spent.gave_up is False
        assert "awaiting the printer" in spent.why

    @pytest.mark.parametrize("sent", [1, 2])
    def test_a_refused_runout_resume_is_owed_again_under_the_cap(self, sent):
        self._row_with(KIND_RUNOUT, *(["refused"] * sent))
        assert incident_resolution.resume_spent(7, 1) is None

    @pytest.mark.parametrize("sent", [1, 2])
    def test_an_unanswered_runout_resume_past_its_bound_is_owed_again_under_the_cap(self, sent):
        """Outstanding, the record passed its self-heal bound (the caller hands those ids in)."""
        self._row_with(KIND_RUNOUT, *(["refused"] * (sent - 1)), None)
        assert incident_resolution.resume_spent(7, 1, unanswered=frozenset({50 + sent - 1})) is None

    def test_the_third_refused_runout_resume_gives_up(self):
        self._row_with(KIND_RUNOUT, "refused", "refused", "refused")
        spent = incident_resolution.resume_spent(7, 1)
        assert spent is not None and spent.gave_up is True
        assert "3 of 3" in spent.why and "refused" in spent.why

    def test_the_third_unanswered_runout_resume_gives_up(self):
        self._row_with(KIND_RUNOUT, "refused", "refused", None)
        spent = incident_resolution.resume_spent(7, 1, unanswered=frozenset({52}))
        assert spent is not None and spent.gave_up is True
        assert "unanswered" in spent.why

    def test_a_reached_resume_spends_the_row_whatever_came_before(self):
        self._row_with(KIND_RUNOUT, "refused", "reached")
        spent = incident_resolution.resume_spent(7, 1)
        assert spent is not None and spent.gave_up is False

    def test_a_person_taking_it_over_spends_it(self):
        self._row_with(KIND_RUNOUT, "refused", "taken_over")
        spent = incident_resolution.resume_spent(7, 1)
        assert spent is not None and spent.gave_up is False

    def test_a_resume_the_printer_never_received_spends_nothing(self):
        """``void`` and a frame that never left the farm are owed again — and never count toward the cap."""
        self._row_with(KIND_RUNOUT, "refused", "void", "refused", "refused", unsent=(3,))
        assert incident_resolution.resume_spent(7, 1) is None

    @pytest.mark.parametrize("kind", [KIND_TOOLHEAD_REFILL, KIND_PHYSICAL])
    def test_every_other_class_is_one_resume_per_row(self, kind):
        """A toolhead row (and a repair row) is not given the runout row's budget: a per-kind-and-job
        budget was ruled out as a new human gate — one refused resume is the give-up, at once."""
        self._row_with(kind, "refused")
        spent = incident_resolution.resume_spent(7, 1)
        assert spent is not None and spent.gave_up is True
        assert "1 of 1" in spent.why


class TestTheResumeOwedVerdict:
    """``resume_owed`` — a row the farm holds over a PAUSEd print whose toolhead the PRINTER or a
    PERSON put back is resumed ONCE (doctrine rule 1; Raymond 2026-10-11: "will i have to click resume
    on a filament swap after this is shipped? the answer should be no"). One total table keyed by the
    resolution class (``_RESUME_OWED``; a missing class RAISES): ``repair`` — the repair evidence (a
    load after the fault, the path quiet, the job PAUSEd); ``toolhead`` — a LEVEL: fed now, no farm load
    on the row reached, no AMS hold beside it; every other class — never. Over it the shared guards:
    maintenance mode, the plate check, a runout row, and the ONCE — the record's farm resume on the
    row."""

    _AFTER = _OPENED_AT + timedelta(seconds=30)
    _BEFORE = _OPENED_AT - timedelta(seconds=30)

    @pytest.fixture(autouse=True)
    def _clean_store(self):
        printer_incidents._reset_state()  # noqa: SLF001
        yield
        printer_incidents._reset_state()  # noqa: SLF001

    def _ctx(self, *, live="PAUSE", tray_now=1, load_at=_AFTER, job=_JOB, hms=None, **kw) -> Context:
        state = _state(live, tray_now=tray_now, hms=hms)
        state.subtask_id = job
        return Context(state=state, observed_load_at=load_at, driver_live=False, feed=_feed_of(state), **kw)

    @staticmethod
    def _held(*rows: dict) -> None:
        """The store's open rows of printer 7, as its projection carries them (``farm_actions`` the
        record's farm actions on each row)."""
        printer_incidents._open_cache[7] = {row["id"]: row for row in rows}  # noqa: SLF001

    @staticmethod
    def _action(command: str, answer: str | None, *, action_id: int = 41, unsent: bool = False) -> dict:
        return {
            "action_id": action_id,
            "command": command,
            "target": None,
            "answer": answer,
            "sent_at": "2026-10-11T03:43:00",
            "answered_at": None if answer is None else "2026-10-11T03:43:05",
            "step_reading": None,
            "reason": None,
            "unsent": unsent,
        }

    def test_the_table_is_total_over_the_resolution_classes(self):
        assert set(incident_resolution._RESUME_OWED) == set(RESOLVES_ON.values())  # noqa: SLF001

    def test_an_unregistered_class_raises(self, monkeypatch):
        monkeypatch.setattr(incident_resolution.printer_incidents, "resolution_class", lambda *a, **k: "invented")

        with pytest.raises(KeyError):
            incident_resolution.resume_owed(_row(RESOLUTION_TOOLHEAD), self._ctx())

    @pytest.mark.parametrize("resolution", [RESOLUTION_TOOLHEAD, RESOLUTION_REPAIR])
    def test_a_toolhead_put_back_on_the_paused_job_is_owed(self, resolution):
        verdict = incident_resolution.resume_owed(_row(resolution), self._ctx())

        assert verdict.owed is True
        assert verdict.evidence

    @pytest.mark.parametrize(
        "resolution", [RESOLUTION_WIRE, RESOLUTION_OPERATOR, RESOLUTION_JOB_PAUSE, RESOLUTION_DECLARED]
    )
    def test_every_other_class_never_resumes_on_a_load(self, resolution):
        verdict = incident_resolution.resume_owed(_row(resolution), self._ctx())

        assert verdict.owed is False
        assert verdict.evidence

    @pytest.mark.parametrize("resolution", [RESOLUTION_TOOLHEAD, RESOLUTION_REPAIR])
    @pytest.mark.parametrize(
        "change",
        [
            pytest.param({"live": "RUNNING"}, id="not_paused"),
            pytest.param({"job": "other-job"}, id="another_job"),
            pytest.param({"job": ""}, id="a_job_naming_nothing"),
            pytest.param({"hms": [_jam_hms()]}, id="a_fault_standing"),
            pytest.param({"command_pending": True}, id="a_farm_command_pending"),
        ],
    )
    def test_never(self, resolution, change):
        assert incident_resolution.resume_owed(_row(resolution), self._ctx(**change)).owed is False

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param({"load_at": _BEFORE}, id="the_load_before_the_row_opened"),
            pytest.param({"load_at": None}, id="no_load_the_farm_did_not_make"),
        ],
    )
    def test_the_repair_class_needs_a_load_after_the_fault(self, change):
        assert incident_resolution.resume_owed(_row(RESOLUTION_REPAIR), self._ctx(**change)).owed is False

    @pytest.mark.parametrize("load_at", [None, _BEFORE], ids=["no_landing_recorded", "a_landing_before_the_hold"])
    def test_a_fed_toolhead_is_owed_without_any_recorded_landing(self, load_at):
        """001-H2S incident 574: the firmware's backup switch landed while the build that opened the
        hold had no record to write it to — after the deploy the toolhead reads FED and no landing is
        recorded after the hold opened. The level answers: owed."""
        verdict = incident_resolution.resume_owed(_row(RESOLUTION_TOOLHEAD), self._ctx(load_at=load_at))

        assert verdict.owed is True

    def test_the_toolhead_must_read_fed(self):
        """A person's load that did not reach the nozzle (or the farm's queued pull-back emptying it
        after) leaves the toolhead empty: no resume onto air."""
        verdict = incident_resolution.resume_owed(_row(RESOLUTION_TOOLHEAD), self._ctx(tray_now=255))

        assert verdict.owed is False
        assert "does not read fed" in verdict.evidence

    def test_the_external_holder_is_fed_for_the_toolhead_class(self):
        """The toolhead class asks only "is it printing air": a person's external spool is fed."""
        assert incident_resolution.resume_owed(_row(RESOLUTION_TOOLHEAD), self._ctx(tray_now=254)).owed is True

    def test_the_farms_own_refill_is_its_episodes_to_resume(self):
        """A farm load on the row that REACHED the toolhead: the refill episode publishes the resume."""
        self._held({"id": 1, "kind": KIND_TOOLHEAD_REFILL, "farm_actions": [self._action("load", "reached")]})

        verdict = incident_resolution.resume_owed(_row(RESOLUTION_TOOLHEAD), self._ctx())

        assert verdict.owed is False
        assert "farm's own refill" in verdict.evidence

    @pytest.mark.parametrize("answer", ["refused", "void"])
    def test_a_farm_load_that_never_reached_leaves_it_owed(self, answer):
        """A failed refill (refused / void) and then a person's load: the toolhead reads fed — owed."""
        self._held({"id": 1, "kind": KIND_TOOLHEAD_REFILL, "farm_actions": [self._action("load", answer)]})

        assert incident_resolution.resume_owed(_row(RESOLUTION_TOOLHEAD), self._ctx()).owed is True

    def test_an_ams_hold_beside_it_answers_the_pause(self):
        self._held({"id": 1, "kind": KIND_TOOLHEAD_REFILL}, {"id": 2, "kind": KIND_JAM})

        verdict = incident_resolution.resume_owed(_row(RESOLUTION_TOOLHEAD), self._ctx())

        assert verdict.owed is False
        assert "AMS hold" in verdict.evidence

    @pytest.mark.parametrize("resolution", [RESOLUTION_TOOLHEAD, RESOLUTION_REPAIR])
    @pytest.mark.parametrize("answer", [None, "reached", "refused", "taken_over"], ids=lambda a: a or "outstanding")
    def test_once_a_farm_resume_on_the_row_spends_it(self, resolution, answer):
        """The ONCE is the record's — a farm resume on the row outstanding, reached, refused by the
        printer or taken over by a person — durable across a restart. One resume per row for every class
        but a runout row's refill (``incident_resolution.resume_spent``, the one decider)."""
        kind = _KIND_BY_CLASS[resolution][0]
        self._held({"id": 1, "kind": kind, "farm_actions": [self._action("resume", answer)]})

        verdict = incident_resolution.resume_owed(_row(resolution), self._ctx())

        assert verdict.owed is False
        spent = incident_resolution.resume_spent(7, 1)
        assert spent is not None and verdict.evidence == spent.why
        assert "action 41" in verdict.evidence
        # A refusal spends a one-resume row with the give-up owed (the page), at once.
        assert spent.gave_up is (answer == "refused")

    @pytest.mark.parametrize("resolution", [RESOLUTION_TOOLHEAD, RESOLUTION_REPAIR])
    @pytest.mark.parametrize(
        "action",
        [
            pytest.param({"answer": "void"}, id="void"),
            pytest.param({"answer": "refused", "unsent": True}, id="never_left_the_farm"),
        ],
    )
    def test_a_resume_the_printer_never_received_is_owed_again(self, resolution, action):
        kind = _KIND_BY_CLASS[resolution][0]
        self._held({"id": 1, "kind": kind, "farm_actions": [self._action("resume", **action)]})

        assert incident_resolution.resume_owed(_row(resolution), self._ctx()).owed is True

    @pytest.mark.parametrize("resolution", [RESOLUTION_TOOLHEAD, RESOLUTION_REPAIR])
    def test_maintenance_mode_resumes_nothing(self, resolution):
        kind = _KIND_BY_CLASS[resolution][0]
        self._held({"id": 1, "kind": kind}, {"id": 3, "kind": KIND_SERVICE_HOLD})

        verdict = incident_resolution.resume_owed(_row(resolution), self._ctx())

        assert verdict.owed is False
        assert "maintenance mode" in verdict.evidence

    @pytest.mark.parametrize("resolution", [RESOLUTION_TOOLHEAD, RESOLUTION_REPAIR])
    def test_a_plate_check_pause_is_its_own_ladders(self, resolution):
        kind = _KIND_BY_CLASS[resolution][0]
        self._held({"id": 1, "kind": kind}, {"id": 4, "kind": KIND_PLATE_VISION})

        verdict = incident_resolution.resume_owed(_row(resolution), self._ctx())

        assert verdict.owed is False
        assert "plate check" in verdict.evidence

    def test_a_runout_row_beside_a_repair_hold_is_the_refill_lanes(self):
        self._held({"id": 1, "kind": KIND_PHYSICAL}, {"id": 5, "kind": KIND_RUNOUT})

        verdict = incident_resolution.resume_owed(_row(RESOLUTION_REPAIR), self._ctx())

        assert verdict.owed is False
        assert "runout row" in verdict.evidence


class TestTheResumeVerdictIsClosed:
    """``resume_owed`` answers ONE of ``owed`` / ``waiting`` / ``declined`` — the spawner acts on ``owed``
    alone; ``waiting`` is a guard the farm itself waits out; a ``declined`` whose pause another open row
    owns names that row's kind (the hold chip shows that row, never "Press Resume")."""

    @pytest.fixture(autouse=True)
    def _clean_store(self):
        printer_incidents._reset_state()  # noqa: SLF001
        yield
        printer_incidents._reset_state()  # noqa: SLF001

    def _ctx(self, *, tray_now=1, hms=None, **kw) -> Context:
        state = _state("PAUSE", tray_now=tray_now, hms=hms)
        state.subtask_id = _JOB
        return Context(state=state, driver_live=False, feed=_feed_of(state), **kw)

    @staticmethod
    def _held(*kinds: str, actions: list | None = None) -> None:
        rows = {1: {"id": 1, "kind": KIND_TOOLHEAD_REFILL, "farm_actions": actions or []}}
        for offset, kind in enumerate(kinds, start=2):
            rows[offset] = {"id": offset, "kind": kind}
        printer_incidents._open_cache[7] = rows  # noqa: SLF001

    def _verdict(self, **kw):
        return incident_resolution.resume_owed(_row(RESOLUTION_TOOLHEAD), self._ctx(**kw))

    def test_owed(self):
        self._held()
        verdict = self._verdict()
        assert (verdict.verdict, verdict.owed, verdict.owner) == ("owed", True, None)

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param({"command_pending": True}, id="a_farm_command_pending"),
            pytest.param({"hms": [_jam_hms()]}, id="the_path_not_quiet"),
        ],
    )
    def test_a_guard_the_farm_waits_out_is_waiting(self, change):
        self._held()
        verdict = self._verdict(**change)
        assert (verdict.verdict, verdict.owed) == ("waiting", False)

    @pytest.mark.parametrize(
        ("kind", "owner"),
        [(KIND_PLATE_VISION, KIND_PLATE_VISION), (KIND_RUNOUT, KIND_RUNOUT), (KIND_JAM, KIND_JAM)],
    )
    def test_another_row_owning_the_pause_is_declined_and_named(self, kind, owner):
        self._held(kind)
        verdict = self._verdict()
        assert (verdict.verdict, verdict.owner) == ("declined", owner)

    def test_maintenance_and_a_spent_once_are_declined_with_no_owner(self):
        self._held(KIND_SERVICE_HOLD)
        assert (self._verdict().verdict, self._verdict().owner) == ("declined", None)
        printer_incidents._reset_state()  # noqa: SLF001
        self._held(actions=[{"action_id": 3, "command": "resume", "answer": "reached"}])
        assert (self._verdict().verdict, self._verdict().owner) == ("declined", None)


def test_a_rebooted_printer_at_the_power_loss_prompt_is_waiting_never_owed():
    """After a reboot the job comes back PAUSEd on its id with the toolhead fed and ``0300_8007``
    standing: the power-loss recovery (``pause_recovery``) owns that resume — the farm waits for it."""
    printer_incidents._reset_state()  # noqa: SLF001
    prompt = HMSError(code="8007", attr=0x03000000, module=3, severity=3, full_code="0300000000008007")
    state = _state("PAUSE", tray_now=1, hms=[prompt])
    state.subtask_id = _JOB
    ctx = Context(state=state, driver_live=False, feed=_feed_of(state))

    verdict = incident_resolution.resume_owed(_row(RESOLUTION_TOOLHEAD), ctx)

    assert (verdict.verdict, verdict.owed) == ("waiting", False)
    assert "power-loss" in verdict.evidence
