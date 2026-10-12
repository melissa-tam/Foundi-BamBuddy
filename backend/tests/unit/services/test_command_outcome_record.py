"""``command_outcome.read`` — the RECORD's outcome tables, one per command family, and the takeover
predicate absorbed from the recovery driver.

Pure: every cell is a function of the action's recorded facts, its running facts and ONE event. The
pins: the table is TOTAL over (family, event kind) and a missing cell RAISES; no cell reads a clock
(the module imports none for the record's reading); each family's characteristic answers.
"""

from __future__ import annotations

import inspect
import itertools
from typing import get_args

import pytest

from backend.app.services import command_outcome
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.command_outcome import (
    ActionFacts,
    Answer,
    DriverReading,
    Ended,
    EventKind,
    Family,
    Landed,
    PersonActed,
    Reading,
    Reported,
    Running,
    SessionBegan,
    WireReading,
    read,
    takeover,
)


def _facts(
    command: str,
    *,
    actor: str = "farm",
    target: int | None = None,
    entry: str | None = None,
    job: str | None = "900",
    prior: str | None = None,
) -> ActionFacts:
    return ActionFacts(
        action_id=1,
        printer_id=7,
        command=command,
        actor=actor,  # type: ignore[arg-type]
        target=target,
        entry=entry,
        job_id=job,
        prior_job_id=prior,
    )


def _reading(
    *, state: str = "PAUSE", tray_now: int = 255, subtask: str | None = "900", epoch: int = 1, rebooted: bool = False
) -> WireReading:
    st = PrinterState()
    st.connected = True
    st.connection_epoch = epoch
    st.report_epoch = epoch
    st.state = state
    st.tray_now = tray_now
    st.subtask_id = subtask
    reading = WireReading.of(st)
    return WireReading(
        gcode_state=reading.gcode_state, subtask_id=reading.subtask_id, ams=reading.ams, rebooted=rebooted
    )


class TestTheTable:
    def test_every_family_answers_every_event_kind(self) -> None:
        assert set(command_outcome._TABLE) == set(itertools.product(Family, get_args(EventKind)))

    def test_a_missing_cell_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delitem(command_outcome._TABLE, (Family.RESUME, "reported"))

        with pytest.raises(LookupError, match="no outcome cell"):
            read(_facts("resume"), Running(), Reported(_reading()))

    def test_every_command_names_a_family(self) -> None:
        for name, spec in command_outcome.COMMANDS.items():
            assert spec.family in Family, name

    def test_an_unknown_command_raises(self) -> None:
        with pytest.raises(LookupError):
            command_outcome.spec_of("purge")

    def test_the_record_reads_no_clock(self) -> None:
        """The record's reading (:func:`read` and its cells) reads no time: an outcome is an event."""
        for name, cell in command_outcome._TABLE.items():
            source = inspect.getsource(cell)
            assert "time." not in source and "monotonic" not in source and "elapsed" not in source, name


class TestJobControl:
    def test_a_fail_ack_refuses(self) -> None:
        outcome = read(_facts("resume"), Running(), command_outcome.Acked(False, "busy"))
        assert outcome.verdict is Answer.REFUSED and outcome.reason == "busy"

    def test_a_success_ack_is_acceptance_never_execution(self) -> None:
        assert read(_facts("resume"), Running(), command_outcome.Acked(True, None)).verdict is Reading.ACKED

    def test_running_reaches_a_resume(self) -> None:
        assert (
            read(_facts("resume"), Running(acked=True), Reported(_reading(state="RUNNING"))).verdict is Answer.REACHED
        )

    def test_another_job_takes_a_resume_over(self) -> None:
        verdict = read(_facts("resume"), Running(), Reported(_reading(state="RUNNING", subtask="901"))).verdict
        assert verdict is Answer.TAKEN_OVER

    def test_a_reboot_voids_a_resume_but_not_the_power_loss_resume(self) -> None:
        rebooted = Reported(_reading(state="PAUSE", rebooted=True))
        assert read(_facts("resume"), Running(), rebooted).verdict is Answer.VOID
        assert read(_facts("power_loss_resume"), Running(), rebooted).verdict is None

    def test_a_power_loss_resume_is_reached_by_running_however_late_and_refused_by_400d(self) -> None:
        """The power-loss resume (2026-10-11): RUNNING reaches it on any later report — no confirm window;
        the firmware's own "Resume failed after power loss" (``0300_400D``) REFUSES it, with the one reason
        the power-loss lane reads its hold copy off."""
        from backend.app.services.bambu_mqtt import HMSError

        assert (
            read(_facts("power_loss_resume"), Running(acked=True), Reported(_reading(state="RUNNING"))).verdict
            is Answer.REACHED
        )
        st = PrinterState()
        st.connected = True
        st.connection_epoch = st.report_epoch = 1
        st.state = "PAUSE"
        st.subtask_id = "900"
        st.hms_errors = [HMSError(code="400D", attr=0x03000000, module=3, severity=2, full_code="030000000000400D")]
        outcome = read(_facts("power_loss_resume"), Running(acked=True), Reported(WireReading.of(st)))
        assert outcome.verdict is Answer.REFUSED
        assert outcome.reason == command_outcome.POWER_LOSS_RESUME_FAILED
        # Still PAUSE with no verdict of the firmware's: nothing is decided — the record waits.
        assert read(_facts("power_loss_resume"), Running(acked=True), Reported(_reading())).verdict is None

    def test_the_job_ending_voids_a_resume(self) -> None:
        assert read(_facts("resume"), Running(), Ended("900", "failed")).verdict is Answer.VOID

    def test_a_stop_is_reached_by_a_terminal_however_late_and_by_a_job_over_word(self) -> None:
        assert read(_facts("stop"), Running(), Ended("900", "failed")).verdict is Answer.REACHED
        assert read(_facts("stop"), Running(), Reported(_reading(state="FAILED"))).verdict is Answer.REACHED

    def test_a_persons_job_control_takes_a_farm_resume_over_never_an_operators(self) -> None:
        assert read(_facts("resume"), Running(), PersonActed(Family.STOP)).verdict is Answer.TAKEN_OVER
        assert read(_facts("resume", actor="operator"), Running(), PersonActed(Family.STOP)).verdict is None

    def test_a_start_is_reached_by_its_job_and_taken_over_by_another(self) -> None:
        start = _facts("print_start", job="424242", prior="900")
        assert read(start, Running(), Reported(_reading(state="PREPARE", subtask="424242"))).verdict is Answer.REACHED
        assert read(start, Running(), Reported(_reading(state="RUNNING", subtask="1"))).verdict is Answer.TAKEN_OVER
        assert read(start, Running(), Reported(_reading(state="IDLE", subtask=None))).verdict is None


class TestTheStartFamilies:
    """A start's job id is MINTED at the send: only a printer that received the start can report it."""

    @pytest.mark.parametrize("command", ["print_start", "eject_start"])
    @pytest.mark.parametrize("word", ["PREPARE", "SLICING", "RUNNING", "PAUSE"])
    def test_the_dispatched_id_in_an_active_word_reaches_it(self, command: str, word: str) -> None:
        """R17 (2026-10-12): production's active set — the words ``_watchdog_print_start`` released a start on."""
        start = _facts(command, job="424242", prior="900")
        verdict = read(start, Running(), Reported(_reading(state=word, subtask="424242"))).verdict
        assert verdict is Answer.REACHED

    @pytest.mark.parametrize(
        ("command", "word"),
        [("print_start", "FAILED"), ("eject_start", "FINISH"), ("eject_start", "FAILED")],
    )
    def test_the_dispatched_id_in_its_terminal_word_reaches_it(self, command: str, word: str) -> None:
        """A print's FAILED with its own id is its terminal (how it ended is the terminal's to classify); a
        short eject sweep can end between two reads, so its FINISH carries the id it ran under."""
        start = _facts(command, job="424242", prior="900")
        verdict = read(start, Running(), Reported(_reading(state=word, subtask="424242"))).verdict
        assert verdict is Answer.REACHED

    @pytest.mark.parametrize(
        ("command", "word"), [("print_start", "IDLE"), ("print_start", "FINISH"), ("eject_start", "IDLE")]
    )
    def test_the_dispatched_id_at_rest_is_a_landing_never_reached(self, command: str, word: str) -> None:
        """#1678 (production Phase B): a printer that took ``project_file`` and stays IDLE / FINISH has not
        started the job. The H2D sits at FINISH ~50 s after taking it (#1078). Non-final: the start stays
        outstanding, and the running facts remember the landing."""
        start = _facts(command, job="424242", prior="900")
        running = Running()
        outcome = read(start, running, Reported(_reading(state=word, subtask="424242")))
        assert (outcome.verdict, outcome.final) == (Reading.LANDED, False)
        assert running.start_landed is True

    def test_slow_h2d_finish_with_our_id_then_prepare_is_reached(self) -> None:
        start = _facts("print_start", job="424242", prior="900")
        running = Running()
        for _ in range(50):
            assert read(start, running, Reported(_reading(state="FINISH", subtask="424242"))).verdict is Reading.LANDED
        assert read(start, running, Reported(_reading(state="PREPARE", subtask="424242"))).verdict is Answer.REACHED

    def test_a_landed_start_is_never_voided_by_a_new_session(self) -> None:
        """Only a printer that received the start can report its id: an unacknowledged start that landed is
        no frame lost on a half-broken session."""
        start = _facts("print_start", job="424242", prior="900")
        running = Running(sent_epoch=1)
        read(start, running, Reported(_reading(state="IDLE", subtask="424242")))
        began = SessionBegan(2, _reading(epoch=2, state="IDLE", subtask="900"))
        assert read(start, running, began).verdict is None

    def test_a_session_begin_that_reads_the_landing_does_not_void_it(self) -> None:
        """A restart's first fresh report re-reads the landing (``Running()`` rehydrated, unacked)."""
        start = _facts("print_start", job="424242", prior="900")
        running = Running()
        began = SessionBegan(2, _reading(epoch=2, state="IDLE", subtask="424242"))
        assert read(start, running, began).verdict is Reading.LANDED
        assert running.start_landed is True

    def test_a_reboot_under_a_landed_start_voids_it(self) -> None:
        start = _facts("print_start", job="424242", prior="900")
        outcome = read(start, Running(), Reported(_reading(state="IDLE", subtask="424242", rebooted=True)))
        assert (outcome.verdict, outcome.reason) == (Answer.VOID, "the printer rebooted")

    def test_the_jobs_terminal_event_reaches_a_landed_start(self) -> None:
        start = _facts("print_start", job="424242", prior="900")
        running = Running()
        read(start, running, Reported(_reading(state="FINISH", subtask="424242")))
        assert read(start, running, Ended("424242", "completed")).verdict is Answer.REACHED

    @pytest.mark.parametrize("command", ["print_start", "eject_start"])
    @pytest.mark.parametrize("word", ["PREPARE", "SLICING"])
    def test_a_frame_still_carrying_the_previous_jobs_id_takes_nothing_over(self, command: str, word: str) -> None:
        start = _facts(command, job="424242", prior="900")
        assert read(start, Running(), Reported(_reading(state=word, subtask="900"))).verdict is None

    @pytest.mark.parametrize("command", ["print_start", "eject_start"])
    def test_another_job_past_the_previous_id_takes_it_over(self, command: str) -> None:
        start = _facts(command, job="424242", prior="900")
        verdict = read(start, Running(), Reported(_reading(state="RUNNING", subtask="555"))).verdict
        assert verdict is Answer.TAKEN_OVER

    def test_with_no_id_at_the_send_no_frame_takes_it_over(self) -> None:
        """The printer reported no job id at the send: no id can be told apart from the job it replaced."""
        start = _facts("print_start", job="424242", prior=None)
        assert read(start, Running(), Reported(_reading(state="RUNNING", subtask="555"))).verdict is None

    def test_another_jobs_terminal_word_takes_nothing_over(self) -> None:
        start = _facts("print_start", job="424242", prior="900")
        assert read(start, Running(), Reported(_reading(state="FINISH", subtask="555"))).verdict is None

    def test_its_own_id_outranks_the_reboot_prompt(self) -> None:
        """The printer reports the dispatched job: it received the start, whatever happened after."""
        start = _facts("print_start", job="424242", prior="900")
        reading = _reading(state="PAUSE", subtask="424242", rebooted=True)
        assert read(start, Running(), Reported(reading)).verdict is Answer.REACHED
        rebooted = _reading(state="IDLE", subtask="900", rebooted=True)
        assert read(start, Running(), Reported(rebooted)).verdict is Answer.VOID

    @pytest.mark.parametrize("command", ["print_start", "eject_start"])
    @pytest.mark.parametrize("busy", ["RUNNING", "PAUSE", "PREPARE"])
    def test_a_start_sent_into_a_busy_job_is_void_when_that_job_ends(self, command: str, busy: str) -> None:
        """2026-08-30: the firmware silently ignores a ``project_file`` sent while it is busy. The busy
        job's own terminal, with the dispatched id never reported, is the event that answers it — VOID,
        where a 180 s start deadline used to guess."""
        start = _facts(command, entry=busy, job="424242", prior="900")
        outcome = read(start, Running(), Ended("900", "completed"))
        assert outcome.verdict is Answer.VOID
        assert "busy with another job" in (outcome.reason or "")

    @pytest.mark.parametrize("entry", ["FINISH", "IDLE", "FAILED", None])
    def test_the_previous_jobs_terminal_after_an_idle_send_answers_nothing(self, entry: str | None) -> None:
        """Sent to a printer at rest (the ordinary dispatch), the previous job's terminal replayed or
        reconciled late says nothing about the start — it may still be coming (#1078: an H2D sat at
        FINISH ~50 s before PREPARE)."""
        start = _facts("eject_start", entry=entry, job="424242", prior="900")
        assert read(start, Running(), Ended("900", "completed")).verdict is None

    def test_its_own_terminal_still_reaches_it(self) -> None:
        start = _facts("eject_start", entry="RUNNING", job="424242", prior="900")
        assert read(start, Running(), Ended("424242", "completed")).verdict is Answer.REACHED


# Each family's command, and a session's first report that shows the state the command was sent for.
_CARRIED_OUT: list[tuple[str, dict[str, object], dict[str, object]]] = [
    ("resume", {}, {"state": "RUNNING"}),
    ("pause", {}, {"state": "PAUSE"}),
    ("power_loss_resume", {}, {"state": "RUNNING"}),
    ("stop", {}, {"state": "IDLE", "subtask": None}),
    ("stop", {}, {"state": "FAILED"}),
    ("print_start", {"job": "424242", "prior": "900"}, {"state": "RUNNING", "subtask": "424242"}),
    ("eject_start", {"job": "424242", "prior": "900"}, {"state": "FINISH", "subtask": "424242"}),
    ("load", {"target": 2, "entry": "idle_empty"}, {"state": "PAUSE", "tray_now": 2}),
    ("unload", {"entry": "assist_loaded"}, {"state": "PAUSE", "tray_now": 255}),
]
# ... and one that shows nothing answering it.
_NOT_ANSWERED: list[tuple[str, dict[str, object], dict[str, object]]] = [
    ("resume", {}, {"state": "PAUSE"}),
    ("pause", {}, {"state": "RUNNING"}),
    ("power_loss_resume", {}, {"state": "PAUSE"}),
    ("stop", {}, {"state": "PAUSE"}),
    ("print_start", {"job": "424242", "prior": "900"}, {"state": "IDLE", "subtask": "900"}),
    ("eject_start", {"job": "424242", "prior": "900"}, {"state": "PREPARE", "subtask": "900"}),
    ("load", {"target": 2, "entry": "idle_empty"}, {"state": "PAUSE", "tray_now": 3}),
]


class TestASessionBeginReadsTheReportFirst:
    """An unacknowledged command the printer CARRIED OUT is answered by the session's first report,
    never voided by it: a start voided there would requeue a unit over its own running print."""

    @pytest.mark.parametrize(("command", "facts", "wire"), _CARRIED_OUT)
    @pytest.mark.parametrize("running", [Running(sent_epoch=1), Running()], ids=["reconnect", "restart"])
    def test_a_report_showing_the_wanted_state_reaches_it(
        self, command: str, facts: dict[str, object], wire: dict[str, object], running: Running
    ) -> None:
        began = SessionBegan(2, _reading(epoch=2, **wire))  # type: ignore[arg-type]
        verdict = read(_facts(command, **facts), Running(sent_epoch=running.sent_epoch), began).verdict  # type: ignore[arg-type]
        assert verdict is Answer.REACHED

    @pytest.mark.parametrize(("command", "facts", "wire"), _NOT_ANSWERED)
    def test_a_report_that_answers_nothing_voids_it(
        self, command: str, facts: dict[str, object], wire: dict[str, object]
    ) -> None:
        began = SessionBegan(2, _reading(epoch=2, **wire))  # type: ignore[arg-type]
        outcome = read(_facts(command, **facts), Running(sent_epoch=1), began)  # type: ignore[arg-type]
        assert outcome.verdict is Answer.VOID
        assert outcome.reason == "a new MQTT session began with the command unacknowledged"

    @pytest.mark.parametrize(("command", "facts", "wire"), _NOT_ANSWERED)
    def test_an_acknowledged_command_keeps_waiting(
        self, command: str, facts: dict[str, object], wire: dict[str, object]
    ) -> None:
        began = SessionBegan(2, _reading(epoch=2, **wire))  # type: ignore[arg-type]
        verdict = read(_facts(command, **facts), Running(acked=True, sent_epoch=1), began).verdict  # type: ignore[arg-type]
        assert verdict is not Answer.VOID

    def test_a_reboot_still_voids_it_with_the_reboots_reason(self) -> None:
        began = SessionBegan(2, _reading(epoch=2, state="PAUSE", rebooted=True))
        outcome = read(_facts("resume"), Running(sent_epoch=1), began)
        assert (outcome.verdict, outcome.reason) == (Answer.VOID, "the printer rebooted")


class TestAmsMotion:
    def test_a_landing_of_the_target_reaches_a_load(self) -> None:
        assert read(_facts("load", target=2), Running(), Landed(2)).verdict is Answer.REACHED
        assert read(_facts("load", target=2), Running(), Landed(3)).verdict is None

    def test_an_unload_sent_loaded_is_reached_when_the_toolhead_reads_empty(self) -> None:
        unload = _facts("unload", entry="assist_loaded")
        assert read(unload, Running(acked=True), Reported(_reading(tray_now=255))).verdict is Answer.REACHED
        assert read(unload, Running(acked=True), Reported(_reading(tray_now=3))).verdict is Reading.HELD

    def test_an_unload_sent_with_nothing_loaded_needs_the_printers_acceptance(self) -> None:
        unload = _facts("unload", entry="idle_empty")
        assert read(unload, Running(sent_epoch=1), Reported(_reading())).verdict is None
        assert read(unload, Running(acked=True, sent_epoch=1), Reported(_reading())).verdict is Answer.REACHED

    def test_a_terminal_does_not_answer_a_motion_command(self) -> None:
        """012-H2S 2026-09-23: held commands ran after the print's terminal."""
        assert read(_facts("load", target=2), Running(acked=True), Ended("900", "failed")).verdict is None

    def test_a_reboot_voids_it(self) -> None:
        verdict = read(_facts("load", target=2), Running(acked=True), Reported(_reading(rebooted=True))).verdict
        assert verdict is Answer.VOID

    def test_a_new_session_voids_only_an_unacknowledged_command(self) -> None:
        began = SessionBegan(2, _reading(epoch=2, tray_now=3))
        assert read(_facts("load", target=2), Running(sent_epoch=1), began).verdict is Answer.VOID
        assert read(_facts("load", target=2), Running(acked=True, sent_epoch=1), began).verdict is Reading.HELD
        assert read(_facts("load", target=2), Running(sent_epoch=2), began).verdict is None

    def test_an_operators_motion_leaves_a_farm_command_outstanding(self) -> None:
        """K11 (production; R13 of 2026-10-12): a person's AMS command answers nothing of the farm's — the
        AMS still runs the farm's accepted command when it runs, so it stays the farm's until an event
        (the toolhead, a reboot) answers it. The person's click is the recovery driver's takeover instead."""
        for family in Family:
            assert read(_facts("unload"), Running(), PersonActed(family)).verdict is None
            assert read(_facts("load", target=2), Running(acked=True), PersonActed(family)).verdict is None


class TestAckAnswered:
    @pytest.mark.parametrize("command", ["ams_control_resume", "clean_print_error", "dialog_press", "gcode_line"])
    def test_the_ack_answers_it(self, command: str) -> None:
        assert read(_facts(command), Running(), command_outcome.Acked(True, None)).verdict is Answer.REACHED
        assert read(_facts(command), Running(), command_outcome.Acked(False, "no")).verdict is Answer.REFUSED


def _driver(**overrides: object) -> DriverReading:
    base: dict[str, object] = {
        "live": "PAUSE",
        "job": "same",
        "reclassified": False,
        "pending_tray_target": None,
        "operator_commanded": False,
        "path_quiet": False,
    }
    base.update(overrides)
    return DriverReading(**base)  # type: ignore[arg-type]


class TestTakeover:
    """``command_outcome.takeover`` — absorbed from ``spool_recovery._takeover``; first match wins."""

    def test_nothing_happened(self) -> None:
        assert takeover(_driver(), awaiting="PAUSE") is None

    def test_the_tokens_in_their_order(self) -> None:
        assert takeover(_driver(reclassified=True, job="other"), awaiting="PAUSE") == "reclassified"
        assert takeover(_driver(job="other"), awaiting="PAUSE") == "job_changed"
        assert takeover(_driver(live="FAILED"), awaiting="PAUSE") == "job_ended"
        assert takeover(_driver(pending_tray_target=4), awaiting="PAUSE", target=2) == "hijacked_load"
        assert takeover(_driver(operator_commanded=True), awaiting="PAUSE") == "operator_command"
        assert takeover(_driver(live="RUNNING"), awaiting="PAUSE") == "resumed"
        assert takeover(_driver(path_quiet=True), awaiting=None, after_contract_resume=True) == "paused_elsewhere"

    def test_the_restart_continuation_owns_a_terminal_but_not_a_job(self) -> None:
        assert takeover(_driver(live="IDLE"), awaiting="job_over") is None
        assert takeover(_driver(live="PREPARE"), awaiting="job_over") == "job_changed"


class TestAMotionCommandWhoseChangeEndedUnfinished:
    """A motion command sent OUTSIDE a filament change whose own change cycle RAN and ENDED without it
    reaching is VOID (:data:`command_outcome.UNFINISHED_CYCLE`) — the AMS ran it and it never will complete
    (an event, never a window: the old driver read it ``acted`` at its step bound). The end is read only
    on a push that applied a FRESH AMS BLOCK (``WireReading.ams_block_at``): ``ams_status`` rides the print
    level and can leave the change on a push without the ``ams`` block, whose ``tray_now`` (the landing)
    arrives one push later. Without the cycle the command stays outstanding (held: it runs on its own,
    011/014-H2S)."""

    @staticmethod
    def _at(ams_status_main: int, block: float, *, tray_now: int = 255) -> WireReading:
        reading = _reading(tray_now=tray_now)
        ams = command_outcome.AmsWireSnapshot(
            tray_now=reading.ams.tray_now,
            ams_status_main=ams_status_main,
            ams_status_sub=reading.ams.ams_status_sub,
            tray_tar=reading.ams.tray_tar,
            connection_epoch=reading.ams.connection_epoch,
            fresh=True,
            taken_at=reading.ams.taken_at,
            feed=reading.ams.feed,
        )
        return WireReading(gcode_state="PAUSE", subtask_id="900", ams=ams, rebooted=False, ams_block_at=block)

    def test_a_load_whose_cycle_ended_without_landing_is_void(self) -> None:
        facts, running = _facts("load", target=1, entry="idle_empty"), Running(acked=True, sent_epoch=1)

        assert not read(facts, running, Reported(self._at(1, 1.0))).final  # the cycle runs
        outcome = read(facts, running, Reported(self._at(0, 2.0)))  # ...and ends; a fresh AMS block, empty

        assert outcome.verdict is Answer.VOID
        assert outcome.reason == command_outcome.UNFINISHED_CYCLE

    def test_a_change_end_on_a_push_without_the_ams_block_waits_for_the_landing(self) -> None:
        """The frame-order pin: ``ams_status`` leaves the change on a print-level delta with NO ``ams``
        block (the block stamp unchanged), and the load's ``tray_now`` lands on the NEXT push — which
        carries the block. Nothing is voided in between; the landing reaches it."""
        facts, running = _facts("load", target=1, entry="idle_empty"), Running(acked=True, sent_epoch=1)

        read(facts, running, Reported(self._at(1, 1.0)))
        assert not read(facts, running, Reported(self._at(0, 1.0))).final  # change ended, no fresh AMS block
        landed = read(facts, running, Reported(self._at(0, 2.0, tray_now=1)))  # the block, with the landing

        assert landed.verdict is Answer.REACHED

    def test_no_cycle_no_void_a_held_command_stays_outstanding(self) -> None:
        facts, running = _facts("load", target=1, entry="idle_empty"), Running(acked=True, sent_epoch=1)

        assert not read(facts, running, Reported(self._at(0, 1.0))).final
        assert not read(facts, running, Reported(self._at(0, 2.0))).final

    def test_a_command_sent_into_a_change_is_never_voided_by_the_change_ending(self) -> None:
        facts, running = _facts("load", target=1, entry="mid_change_empty"), Running(acked=True, sent_epoch=1)

        read(facts, running, Reported(self._at(1, 1.0)))
        assert not read(facts, running, Reported(self._at(0, 2.0))).final  # the print's change released; held runs


class TestTheRebootVoidsOnlyWhatWasSentBeforeIt:
    """The power-loss prompt is a LEVEL that stands until the printer is resumed: a command the farm sent
    while it already stood was sent after the reboot, whose emptied queue it is not in — the prompt voids
    it no more (``Running.rebooted_at_send``). A command sent before the prompt appeared is void; one whose
    send-time reading is unknown (a farm restart) reads the prompt as the reboot under it."""

    def test_a_command_sent_before_the_reboot_is_void(self) -> None:
        running = Running(acked=True, sent_epoch=1, rebooted_at_send=False)

        verdict = read(_facts("load", target=2), running, Reported(_reading(rebooted=True))).verdict

        assert verdict is Answer.VOID

    def test_a_command_sent_while_the_prompt_stood_is_not_voided_by_it(self) -> None:
        running = Running(acked=True, sent_epoch=1, rebooted_at_send=True)

        assert read(_facts("load", target=2), running, Reported(_reading(rebooted=True))).verdict is not Answer.VOID
        landed = read(_facts("load", target=2), running, Reported(_reading(tray_now=2, rebooted=True)))
        assert landed.verdict is Answer.REACHED

    def test_unknown_at_the_send_reads_the_prompt_as_the_reboot(self) -> None:
        verdict = read(_facts("resume"), Running(), Reported(_reading(state="PAUSE", rebooted=True))).verdict

        assert verdict is Answer.VOID
