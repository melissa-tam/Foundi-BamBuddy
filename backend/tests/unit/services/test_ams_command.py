"""``services/ams_command`` — ONE owner for "an AMS motion command and what the wire answered".

The 2026-09-11 premise ("the firmware drops every load and unload while the AMS is mid
filament-change") was asserted, not measured per posture, and two refusals were built on
it. This suite pins the measurement that replaces it:

* the classifier table — every ``(command, posture)`` row against every answer it can
  give, the ``session_changed`` precedence, the grace-held completions, the
  ``mid_change_empty`` undecidable short-circuit, ``held`` (acknowledged + unmoved, the
  012-H2S 2026-09-23 measurement) in exactly the mid-change rows that can move,
  ``acted`` vs ``no_movement`` at the deadline, and a missing row RAISING (the
  ``incident_resolution`` pattern);
* the ACK correlation — the firmware's ACK is read for the send's OWN sequence id;
* the verbs — the only two refusals kept, the pre-send mark and snapshot ordering, the
  send's sequence id, the operator attribution stamp and its session-epoch scope, a
  publish that did not go out;
* the operator facade's outcome mapping and its log lines;
* the single-caller pin: only ``ams_command`` calls the client's motion publishers.
"""

from __future__ import annotations

import ast
import itertools
import logging
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest

from backend.app.models.printer_incident_step import STEP_KIND_COMMAND, STEP_KIND_LEVER
from backend.app.schemas.printer import AmsCommandOutcome
from backend.app.services import ams_command, spool_respool
from backend.app.services.ams_command import (
    OPERATOR_ACK_S,
    UNLOAD_GRACE_S,
    AmsCommandResult,
    AmsWireSnapshot,
    Answer,
    Command,
    Observation,
    Posture,
    PostureFamily,
    Refusal,
    Sent,
    ack_of,
    classify,
    command_for_operator,
    observe,
    operator_commanded_since,
    posture,
    ran,
    snapshot,
)
from backend.app.services.bambu_mqtt import CommandAck, PrinterState, ams_mid_filament_change
from backend.app.services.printer_incidents import StepEntry
from backend.app.services.tray_fields import ExtruderFeed, ToolheadFeed, extruder_feed, toolhead_feed
from backend.tests._fixtures.ast_tree import ParsedModule, ParsedTree
from backend.tests._fixtures.clock import FakeClock

_LOGGER = "backend.app.services.ams_command"
_PID = 12
_DRIVER_DEADLINE_S = 90.0

# 0700_8011 — the slot-agnostic "insert into the SAME slot" runout, and
# 0700_2200_0002_0001 — "AMS A Slot 3 filament has run out" (a slot-attributed DEMAND).
_BARE_8011 = SimpleNamespace(attr=0x07000000, code="0x8011", full_code="0700000000008011")
_SLOT3_DEMAND = SimpleNamespace(attr=0x07002200, code="0x20001", full_code="0700220000020001")


@pytest.fixture(autouse=True)
def _fresh_operator_stamps() -> Iterator[None]:
    ams_command._reset_state()
    yield
    ams_command._reset_state()


def _snap(
    *,
    tray_now: int | None = 255,
    main: int | None = 0,
    sub: int | None = 0,
    tray_tar: int | None = None,
    epoch: int = 1,
    fresh: bool = True,
) -> AmsWireSnapshot:
    return AmsWireSnapshot(
        tray_now=tray_now,
        ams_status_main=main,
        ams_status_sub=sub,
        tray_tar=tray_tar,
        connection_epoch=epoch,
        fresh=fresh,
        taken_at=0.0,
        feed=extruder_feed(tray_now),
    )


def _with(snap: AmsWireSnapshot, **fields: int | bool | None) -> AmsWireSnapshot:
    """``dataclasses.replace`` that keeps the K1 ``feed`` in step with a changed ``tray_now``
    (the single-nozzle reading :func:`_snap` builds)."""
    out = replace(snap, **fields)
    return replace(out, feed=extruder_feed(out.tray_now)) if "tray_now" in fields else out


def _ack(result: str | None, *, seq: str = "7", command: str = "ams_change_filament") -> CommandAck:
    """One firmware ACK as ``bambu_mqtt`` records it."""
    return CommandAck(command=command, sequence_id=seq, result=result, reason=None, at=0.0)


def _run(
    command: Command,
    target: int | None,
    entry: AmsWireSnapshot,
    polls: list[tuple[float, AmsWireSnapshot]],
    *,
    deadline_s: float = _DRIVER_DEADLINE_S,
    ack: CommandAck | None = None,
    ack_from_s: float = 0.0,
) -> tuple[Answer | None, float | None]:
    """Feed the polls through ONE observation, as a caller's loop does — folding the
    send's ACK before every call, the ACK visible from ``ack_from_s`` on; return the
    first answer and the elapsed time it landed at."""
    observation = Observation()
    for elapsed_s, now in polls:
        observation.fold_ack(ack if elapsed_s >= ack_from_s else None)
        answer = classify(
            command, target, entry, now, observation=observation, elapsed_s=elapsed_s, deadline_s=deadline_s
        )
        if answer is not None:
            return answer, elapsed_s
    return None, None


def _held(now: AmsWireSnapshot, *times: float) -> list[tuple[float, AmsWireSnapshot]]:
    return [(t, now) for t in times]


# --- posture -----------------------------------------------------------------------------


class TestPosture:
    @pytest.mark.parametrize(
        "snap, expected",
        [
            pytest.param(_snap(main=0, tray_now=255), "idle_empty", id="idle_empty"),
            pytest.param(_snap(main=0, tray_now=2), "idle_loaded", id="idle_loaded"),
            pytest.param(_snap(main=0, tray_now=254), "idle_empty", id="idle_external_is_no_feeder"),
            pytest.param(_snap(main=3, tray_now=2), "assist_loaded", id="assist_loaded_is_a_running_h2s"),
            pytest.param(_snap(main=3, tray_now=255), "assist_empty", id="assist_empty_the_011_posture_after"),
            pytest.param(_snap(main=1, tray_now=3), "mid_change_loaded", id="mid_change_real_feeder"),
            pytest.param(_snap(main=1, tray_now=255), "mid_change_empty", id="mid_change_nothing_fed"),
            pytest.param(_snap(main=1, tray_now=254), "mid_change_empty", id="mid_change_external_is_no_feeder"),
            pytest.param(_snap(main=1, tray_now=None), "mid_change_empty", id="mid_change_unread_tray"),
            pytest.param(_snap(main=2, tray_now=2), "other_loaded", id="identifying"),
            pytest.param(_snap(main=4, tray_now=2), "other_loaded", id="calibration"),
            pytest.param(_snap(main=4, tray_now=255), "other_empty", id="calibration_empty"),
            pytest.param(_snap(main=None, tray_now=None), "other_empty", id="unreadable_state"),
        ],
    )
    def test_posture_of_a_snapshot(self, snap: AmsWireSnapshot, expected: Posture) -> None:
        assert posture(snap) == expected
        # The two mid-change postures are exactly what a mid filament-change snapshot
        # reads as — the one-origin predicate decides membership, case by case.
        assert (posture(snap) in {"mid_change_loaded", "mid_change_empty"}) is ams_mid_filament_change(snap)

    def test_every_posture_is_named_in_the_type(self) -> None:
        assert set(get_args(Posture)) == {
            f"{family}_{feed}" for family in ("idle", "assist", "other", "mid_change") for feed in ("loaded", "empty")
        }

    @pytest.mark.parametrize(
        "active, expected",
        [
            pytest.param(0, "assist_loaded", id="the_loaded_right_nozzle_is_active"),
            pytest.param(1, "assist_empty", id="the_empty_left_nozzle_is_active"),
        ],
    )
    def test_a_dual_nozzle_entry_feeder_is_the_active_extruders(self, active: int, expected: Posture) -> None:
        """K1: the entry feeder is the ACTIVE extruder's own (``h2d_extruder_snow``), not the
        single ``tray_now`` the client guessed onto a unit — here still naming slot 5 from
        the right nozzle while the left one is the active, empty one."""
        state = PrinterState(
            tray_now=5, ams_status_main=3, h2d_extruder_snow={0: 5, 1: 255}, active_extruder=active, connection_epoch=1
        )

        assert posture(snapshot(state)) == expected


class TestSnapshot:
    def test_reads_the_five_wire_fields(self) -> None:
        state = PrinterState(tray_now=3, ams_status_main=1, ams_status_sub=5, tray_tar=3, connection_epoch=4)

        snap = snapshot(state)

        assert (snap.tray_now, snap.ams_status_main, snap.ams_status_sub, snap.tray_tar, snap.connection_epoch) == (
            3,
            1,
            5,
            3,
            4,
        )

    def test_no_state_is_all_none(self) -> None:
        snap = snapshot(None)

        assert (snap.tray_now, snap.ams_status_main, snap.ams_status_sub, snap.tray_tar, snap.connection_epoch) == (
            None,
            None,
            None,
            None,
            None,
        )

    def test_a_state_without_the_fields_is_all_none(self) -> None:
        snap = snapshot(SimpleNamespace(state="PAUSE"))

        assert snap.tray_now is None and snap.ams_status_main is None and snap.connection_epoch is None
        assert snap.fresh is False

    @pytest.mark.parametrize(
        "connected, report_epoch, fresh",
        [
            pytest.param(True, 4, True, id="the_current_sessions_report_applied"),
            pytest.param(True, None, False, id="a_new_session_before_its_first_report"),
            pytest.param(True, 3, False, id="the_previous_sessions_report"),
            pytest.param(False, 4, False, id="a_dropped_session"),
        ],
    )
    def test_fresh_is_the_one_freshness_question(self, connected: bool, report_epoch: int | None, fresh: bool) -> None:
        """``print_reconcile.is_fresh`` — connected, and THIS session's report applied."""
        state = PrinterState(connected=connected, connection_epoch=4, report_epoch=report_epoch)

        assert snapshot(state).fresh is fresh
        assert snapshot(None).fresh is False


# --- the classifier table ------------------------------------------------------------------


class TestClassifierTable:
    def test_the_table_spells_out_every_command_posture_pair(self) -> None:
        assert set(ams_command._ROWS) == set(itertools.product(get_args(Command), get_args(Posture)))

    def test_a_missing_row_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delitem(ams_command._ROWS, ("unload", "idle_loaded"))

        with pytest.raises(LookupError, match=r"no classifier row for \(command='unload', posture='idle_loaded'\)"):
            _run("unload", None, _snap(main=0, tray_now=3), [(1.0, _snap(main=0, tray_now=3))])

    def test_an_unknown_command_raises_rather_than_borrowing_a_row(self) -> None:
        with pytest.raises(LookupError, match="no classifier row"):
            classify(
                "purge",  # type: ignore[arg-type]
                None,
                _snap(),
                _snap(),
                observation=Observation(),
                elapsed_s=1.0,
                deadline_s=_DRIVER_DEADLINE_S,
            )

    def test_the_answers_plus_the_two_refusals_are_the_wire_outcome_type(self) -> None:
        assert set(get_args(Answer)) | {"refused_not_connected", "refused_runout_hold"} == set(
            get_args(AmsCommandOutcome)
        )


# Entry snapshots, one per posture.
_ENTRY: dict[Posture, AmsWireSnapshot] = {
    "idle_loaded": _snap(main=0, tray_now=3),
    "idle_empty": _snap(main=0, tray_now=255),
    "assist_loaded": _snap(main=3, tray_now=3),
    "assist_empty": _snap(main=3, tray_now=255),
    "other_loaded": _snap(main=4, tray_now=3),
    "other_empty": _snap(main=4, tray_now=255),
    "mid_change_loaded": _snap(main=1, sub=5, tray_now=3),
    "mid_change_empty": _snap(main=1, sub=5, tray_now=255),
}


# The postures outside a filament change, by entry feeder.
_OUTSIDE_LOADED: list[Posture] = ["idle_loaded", "assist_loaded", "other_loaded"]
_OUTSIDE_EMPTY: list[Posture] = ["idle_empty", "assist_empty", "other_empty"]


def test_every_posture_has_an_entry_snapshot_that_reads_as_it() -> None:
    assert {posture(entry) for entry in _ENTRY.values()} == set(get_args(Posture))
    assert all(posture(entry) == name for name, entry in _ENTRY.items())


def _new_session(entry: AmsWireSnapshot, *, fresh: bool, **fields: int | None) -> AmsWireSnapshot:
    """A reading on the session AFTER the entry's: ``fresh`` False is the new session's
    cache (the old session's fields, before its first report lands)."""
    return _with(entry, connection_epoch=(entry.connection_epoch or 0) + 1, fresh=fresh, **fields)


class TestSessionChanged:
    """A command read on a NEWER session (F4, 2026-09-25). It used to answer
    ``session_changed`` at once in every row — so a load that completed across a reconnect
    read as unknowable, and the recovery driver aborted on it. Now the new session is
    waited out to its first FRESH report and the command is answered by its motion
    (:data:`ams_command._ACROSS_ROWS`)."""

    def test_the_cross_session_table_spells_out_every_command_posture_pair(self) -> None:
        assert set(ams_command._ACROSS_ROWS) == set(itertools.product(get_args(Command), get_args(Posture)))

    def test_a_missing_cross_session_row_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delitem(ams_command._ACROSS_ROWS, ("load", "idle_loaded"))
        entry = _ENTRY["idle_loaded"]

        with pytest.raises(LookupError, match=r"no cross-session row for \(command='load', posture='idle_loaded'\)"):
            _run("load", 5, entry, [(1.0, _new_session(entry, fresh=True))])

    @pytest.mark.parametrize("command, entry_posture", sorted(ams_command._ROWS))
    def test_a_new_session_that_is_not_fresh_is_waited_out_then_session_changed_at_the_deadline(
        self, command: Command, entry_posture: Posture
    ) -> None:
        """The new session's fields are the old session's cache — even a reading that
        WOULD be ``complete`` (a load's tray_now on target) answers nothing yet."""
        entry = _ENTRY[entry_posture]
        cached = _new_session(entry, fresh=False, tray_now=5, ams_status_main=0)

        assert _run(command, 5, entry, [(0.5, cached), (_DRIVER_DEADLINE_S - 0.1, cached)]) == (None, None)
        assert _run(command, 5, entry, [(0.5, cached), (_DRIVER_DEADLINE_S, cached)]) == (
            "session_changed",
            _DRIVER_DEADLINE_S,
        )

    @pytest.mark.parametrize("entry_posture", get_args(Posture))
    def test_a_load_whose_feeder_reads_the_target_on_the_first_fresh_report_is_complete(
        self, entry_posture: Posture
    ) -> None:
        entry = _ENTRY[entry_posture]

        assert _run("load", 5, entry, [(0.5, _new_session(entry, fresh=True, tray_now=5))]) == ("complete", 0.5)

    @pytest.mark.parametrize("entry_posture", [*_OUTSIDE_LOADED, *_OUTSIDE_EMPTY, "mid_change_loaded"])
    def test_an_unload_whose_ams_reads_idle_and_empty_on_the_first_fresh_report_is_complete(
        self, entry_posture: Posture
    ) -> None:
        entry = _ENTRY[entry_posture]
        now = _new_session(entry, fresh=True, tray_now=255, ams_status_main=0)

        assert _run("unload", None, entry, [(0.5, now)]) == ("complete", 0.5)

    def test_an_unload_sent_with_nothing_loaded_stays_undecidable_on_any_session(self) -> None:
        """No filament was at the feeder for it to move: an idle, empty AMS afterwards is
        not its answer."""
        entry = _ENTRY["mid_change_empty"]
        now = _new_session(entry, fresh=True, tray_now=255, ams_status_main=0)

        assert _run("unload", None, entry, [(0.5, now)]) == ("undecidable", 0.5)

    def test_a_mid_change_empty_reading_is_not_an_unloads_completion_across_a_reconnect(self) -> None:
        """Idle AND empty is the strict reading: a 255 inside the change the AMS still
        owes is motion (``acted``), never proof across a gap nobody watched."""
        entry = _ENTRY["mid_change_loaded"]
        now = _new_session(entry, fresh=True, tray_now=255)

        assert _run("unload", None, entry, [(0.5, now)]) == ("acted", 0.5)

    @pytest.mark.parametrize("command", ["load", "unload"])
    def test_a_moved_field_short_of_completion_is_acted(self, command: Command) -> None:
        entry = _ENTRY["idle_loaded"]
        now = _new_session(entry, fresh=True, ams_status_sub=4)

        assert _run(command, 5, entry, [(0.5, now)]) == ("acted", 0.5)

    def test_motion_seen_on_the_old_session_still_counts_after_the_reconnect(self) -> None:
        entry = _ENTRY["idle_loaded"]
        polls = [(0.5, replace(entry, ams_status_sub=4)), (1.0, _new_session(entry, fresh=True))]

        assert _run("load", 5, entry, polls) == ("acted", 1.0)

    @pytest.mark.parametrize("command", ["load", "unload"])
    @pytest.mark.parametrize("entry_posture", [*_OUTSIDE_LOADED, "mid_change_loaded"])
    def test_nothing_moved_is_session_changed_the_honest_cannot_tell(
        self, command: Command, entry_posture: Posture
    ) -> None:
        entry = _ENTRY[entry_posture]

        assert _run(command, 5, entry, [(0.5, _new_session(entry, fresh=True))]) == ("session_changed", 0.5)

    @pytest.mark.parametrize("command", ["load", "unload"])
    def test_an_acknowledged_unmoved_command_is_never_held_across_a_reconnect(self, command: Command) -> None:
        """The ACK was read on the session that is gone: held is a same-session answer."""
        entry = _ENTRY["mid_change_loaded"]
        polls = [(UNLOAD_GRACE_S + 1.0, _new_session(entry, fresh=True))]

        assert _run(command, 5, entry, polls, ack=_ack("success")) == ("session_changed", UNLOAD_GRACE_S + 1.0)

    def test_a_lost_state_is_not_evidence_and_reads_session_changed_at_the_deadline(self) -> None:
        """No state at all (``snapshot(None)``: no epoch, not fresh) used to answer
        ``session_changed`` on the spot; it is waited out like any new session's cache."""
        entry = _ENTRY["mid_change_loaded"]

        assert _run("unload", None, entry, [(1.0, snapshot(None))]) == (None, None)
        assert _run("unload", None, entry, [(1.0, snapshot(None)), (_DRIVER_DEADLINE_S, snapshot(None))]) == (
            "session_changed",
            _DRIVER_DEADLINE_S,
        )


class TestUnloadOutsideAChange:
    """``(unload, idle | assist | other, loaded | empty)`` — the recovery driver's two
    evidence paths."""

    @pytest.mark.parametrize("entry_posture", [*_OUTSIDE_LOADED, *_OUTSIDE_EMPTY])
    def test_a_cycle_then_idle_and_empty_is_complete_at_once(self, entry_posture: Posture) -> None:
        entry = _ENTRY[entry_posture]
        polls = [(1.0, _snap(main=1, sub=4, tray_now=3)), (3.0, _snap(main=0, tray_now=255))]

        assert _run("unload", None, entry, polls) == ("complete", 3.0)

    @pytest.mark.parametrize("entry_posture", _OUTSIDE_EMPTY)
    def test_no_cycle_seen_needs_idle_and_empty_held_for_the_grace(self, entry_posture: Posture) -> None:
        entry = _ENTRY[entry_posture]
        settled = _snap(main=0, tray_now=255)

        assert _run("unload", None, entry, _held(settled, 1.0, 1.0 + UNLOAD_GRACE_S - 0.1)) == (None, None)
        assert _run("unload", None, entry, _held(settled, 1.0, 1.0 + UNLOAD_GRACE_S)) == (
            "complete",
            1.0 + UNLOAD_GRACE_S,
        )

    def test_a_contrary_poll_restarts_the_grace(self) -> None:
        entry = _snap(main=0, tray_now=255)
        settled = _snap(main=0, tray_now=255)
        polls = [(0.0, settled), (10.0, settled), (11.0, _snap(main=0, tray_now=3)), (12.0, settled)]

        assert _run("unload", None, entry, [*polls, (26.9, settled)]) == (None, None)
        assert _run("unload", None, entry, [*polls, (27.0, settled)]) == ("complete", 27.0)

    def test_nothing_fed_alone_is_never_completion(self) -> None:
        """After a feed fault ``tray_now`` already reads 255 before the unload
        (009-H2S 2026-07-20): an AMS still busy at the deadline is not unloaded."""
        entry = _snap(main=3, tray_now=255)
        busy = _snap(main=3, tray_now=255)

        assert _run("unload", None, entry, _held(busy, 1.0, 50.0, _DRIVER_DEADLINE_S)) == (
            "no_movement",
            _DRIVER_DEADLINE_S,
        )

    # Not ``idle_empty``: a stirred idle, empty AMS is the dwell's completion (above).
    @pytest.mark.parametrize("entry_posture", [*_OUTSIDE_LOADED, "assist_empty", "other_empty"])
    def test_a_moved_field_without_completion_is_acted_at_the_deadline(self, entry_posture: Posture) -> None:
        entry = _ENTRY[entry_posture]
        stirred = replace(entry, ams_status_sub=(entry.ams_status_sub or 0) + 1)

        assert _run("unload", None, entry, _held(stirred, 1.0, _DRIVER_DEADLINE_S - 0.1)) == (None, None)
        assert _run("unload", None, entry, _held(stirred, 1.0, _DRIVER_DEADLINE_S)) == ("acted", _DRIVER_DEADLINE_S)

    @pytest.mark.parametrize("entry_posture", _OUTSIDE_LOADED)
    def test_nothing_moved_is_no_movement_at_the_deadline(self, entry_posture: Posture) -> None:
        """No ACK read: nothing says the AMS accepted the pull-back, so nothing is held."""
        entry = _ENTRY[entry_posture]

        assert _run("unload", None, entry, _held(entry, 1.0, _DRIVER_DEADLINE_S)) == (
            "no_movement",
            _DRIVER_DEADLINE_S,
        )

    def test_movement_is_sticky(self) -> None:
        """A field that moved and moved back still moved."""
        entry = _ENTRY["idle_loaded"]
        polls = [(1.0, _snap(main=0, tray_now=3, tray_tar=3)), (2.0, entry), (_DRIVER_DEADLINE_S, entry)]

        assert _run("unload", None, entry, polls) == ("acted", _DRIVER_DEADLINE_S)


class TestUnloadMidChangeLoaded:
    """``(unload, mid_change_loaded)`` — the 012-H2S posture (a real feeder loaded, a
    mechanical fault, the AMS in state 1). Never measured before this module."""

    def test_the_feeder_left_for_nothing_fed_and_held_is_complete_while_still_in_state_1(self) -> None:
        entry = _ENTRY["mid_change_loaded"]
        emptied = _snap(main=1, sub=5, tray_now=255)

        assert _run("unload", None, entry, _held(emptied, 2.0, 2.0 + UNLOAD_GRACE_S - 0.1)) == (None, None)
        assert _run("unload", None, entry, _held(emptied, 2.0, 2.0 + UNLOAD_GRACE_S)) == (
            "complete",
            2.0 + UNLOAD_GRACE_S,
        )

    def test_the_feeder_coming_back_restarts_the_grace(self) -> None:
        entry = _ENTRY["mid_change_loaded"]
        emptied = _snap(main=1, sub=5, tray_now=255)
        polls = [(2.0, emptied), (10.0, _snap(main=1, sub=5, tray_now=3)), (11.0, emptied)]

        assert _run("unload", None, entry, [*polls, (25.9, emptied)]) == (None, None)
        assert _run("unload", None, entry, [*polls, (26.0, emptied)]) == ("complete", 26.0)

    def test_an_empty_reading_inside_a_short_window_is_acted(self) -> None:
        """The operator's window is shorter than the grace: the edge is movement, not yet
        a held completion."""
        entry = _ENTRY["mid_change_loaded"]
        emptied = _snap(main=1, sub=5, tray_now=255)

        assert _run("unload", None, entry, _held(emptied, 1.0, OPERATOR_ACK_S), deadline_s=OPERATOR_ACK_S) == (
            "acted",
            OPERATOR_ACK_S,
        )

    def test_nothing_moved_is_no_movement_at_the_deadline(self) -> None:
        entry = _ENTRY["mid_change_loaded"]

        assert _run("unload", None, entry, _held(entry, 1.0, 50.0, _DRIVER_DEADLINE_S)) == (
            "no_movement",
            _DRIVER_DEADLINE_S,
        )


class TestUnloadMidChangeEmpty:
    """``(unload, mid_change_empty)`` — nothing loaded, so nothing physical can answer."""

    def test_undecidable_at_the_grace_never_the_full_deadline(self) -> None:
        entry = _ENTRY["mid_change_empty"]

        assert _run("unload", None, entry, _held(entry, 1.0, UNLOAD_GRACE_S - 0.1)) == (None, None)
        assert _run("unload", None, entry, _held(entry, 1.0, UNLOAD_GRACE_S)) == ("undecidable", UNLOAD_GRACE_S)

    def test_undecidable_at_a_deadline_shorter_than_the_grace(self) -> None:
        """The operator's window: a mid-change-empty unload must not read ``no_movement``
        (which says the AMS could have moved and did not)."""
        entry = _ENTRY["mid_change_empty"]

        assert _run("unload", None, entry, _held(entry, 1.0, OPERATOR_ACK_S), deadline_s=OPERATOR_ACK_S) == (
            "undecidable",
            OPERATOR_ACK_S,
        )

    def test_a_movement_is_not_undecidable_it_is_acted_at_the_deadline(self) -> None:
        entry = _ENTRY["mid_change_empty"]
        stirred = _snap(main=1, sub=6, tray_now=255)

        assert _run("unload", None, entry, _held(stirred, 1.0, UNLOAD_GRACE_S, 50.0)) == (None, None)
        assert _run("unload", None, entry, _held(stirred, 1.0, UNLOAD_GRACE_S, _DRIVER_DEADLINE_S)) == (
            "acted",
            _DRIVER_DEADLINE_S,
        )


class TestLoadInEveryPosture:
    """``(load, any posture)`` — complete on ``tray_now == target``."""

    @pytest.mark.parametrize("entry_posture", get_args(Posture))
    def test_reaching_the_target_is_complete(self, entry_posture: Posture) -> None:
        entry = _ENTRY[entry_posture]
        polls = [(1.0, _with(entry, tray_now=255)), (4.0, _with(entry, tray_now=5, ams_status_main=0))]

        assert _run("load", 5, entry, polls) == ("complete", 4.0)

    @pytest.mark.parametrize("entry_posture", get_args(Posture))
    def test_moving_without_reaching_the_target_is_acted_at_the_deadline(self, entry_posture: Posture) -> None:
        entry = _ENTRY[entry_posture]
        stirred = replace(entry, tray_tar=5)

        assert _run("load", 5, entry, _held(stirred, 1.0, _DRIVER_DEADLINE_S)) == ("acted", _DRIVER_DEADLINE_S)

    @pytest.mark.parametrize("entry_posture", get_args(Posture))
    def test_nothing_moved_is_no_movement_at_the_deadline(self, entry_posture: Posture) -> None:
        entry = _ENTRY[entry_posture]

        assert _run("load", 5, entry, _held(entry, 1.0, _DRIVER_DEADLINE_S - 0.1)) == (None, None)
        assert _run("load", 5, entry, _held(entry, 1.0, _DRIVER_DEADLINE_S)) == ("no_movement", _DRIVER_DEADLINE_S)

    def test_the_nothing_fed_sentinel_is_never_a_completed_load(self) -> None:
        """Ext-R's tray id is 255 — a ``tray_now`` of 255 is the absence of a load."""
        entry = _snap(main=0, tray_now=255)

        assert _run("load", 255, entry, _held(entry, 1.0, _DRIVER_DEADLINE_S)) == ("no_movement", _DRIVER_DEADLINE_S)

    def test_a_load_of_the_external_spool_completes_on_the_external_holder_feeding(self) -> None:
        entry = _snap(main=0, tray_now=255)

        assert _run("load", 254, entry, [(2.0, _snap(main=0, tray_now=254))]) == ("complete", 2.0)


def _dual(snap: AmsWireSnapshot, active: ExtruderFeed) -> AmsWireSnapshot:
    """A dual-nozzle reading: ``tray_now`` is the client's single value, guessed onto a unit,
    while the ACTIVE extruder's own feed (``h2d_extruder_snow``) says something else."""
    return replace(snap, feed=active)


class TestCompletionReadsTheActiveExtruder:
    """K1: what the AMS answered a motion command is read off the ACTIVE extruder's feed
    (``AmsWireSnapshot.feed``, ``tray_fields.toolhead_feed``) — the reading the posture, ``ran``
    and the refill's confirm already take — never the single ``tray_now`` a dual-nozzle client
    had to guess onto an AMS unit."""

    def test_a_load_completes_when_the_active_extruder_reads_the_target(self) -> None:
        entry = _snap(main=0, tray_now=255)
        now = _dual(_snap(main=0, tray_now=5), ExtruderFeed("fed", 3))

        assert _run("load", 3, entry, [(2.0, now)]) == ("complete", 2.0)

    def test_a_tray_now_on_target_over_an_empty_active_extruder_is_no_load(self) -> None:
        entry = _snap(main=0, tray_now=255)
        now = _dual(_snap(main=0, tray_now=3), ExtruderFeed("empty"))

        assert _run("load", 3, entry, [(2.0, now)]) == (None, None)

    def test_an_unload_settles_on_the_active_extruder_reading_empty(self) -> None:
        entry = _snap(main=0, tray_now=5)
        settled = _dual(_snap(main=0, tray_now=5), ExtruderFeed("empty"))

        assert _run("unload", None, entry, _held(settled, 1.0, 1.0 + UNLOAD_GRACE_S)) == (
            "complete",
            1.0 + UNLOAD_GRACE_S,
        )

    def test_an_unload_read_across_a_reconnect_reads_the_active_extruder(self) -> None:
        entry = _snap(main=0, tray_now=5)
        now = _dual(_new_session(entry, fresh=True), ExtruderFeed("empty"))

        assert _run("unload", None, entry, [(0.5, now)]) == ("complete", 0.5)


class TestFoldAck:
    """``Observation.fold_ack`` — the firmware's ACK for THIS send, folded by the caller."""

    def test_unseen_is_none(self) -> None:
        observation = Observation()

        observation.fold_ack(None)

        assert observation.acked is None

    @pytest.mark.parametrize("result", ["success", "SUCCESS", "ok", "OK", "Success"])
    def test_the_success_spellings(self, result: str) -> None:
        """The wire has shown ``result=success`` (motion echoes) and ``result=SUCCESS``
        (the ``ams_control resume`` echo), 012-H2S 2026-09-23."""
        observation = Observation()

        observation.fold_ack(_ack(result))

        assert observation.acked is True

    @pytest.mark.parametrize("result", ["fail", "failed", "busy", "", None])
    def test_anything_else_is_a_failure(self, result: str | None) -> None:
        observation = Observation()

        observation.fold_ack(_ack(result))

        assert observation.acked is False

    def test_the_reading_is_the_ack_records_own(self) -> None:
        """ONE reading of ``result``: ``CommandAck.succeeded``. ``ams_command`` keeps no
        spelling set of its own, so the motion lane and the dialog-button lane can never
        disagree about what the firmware said."""
        from backend.app.services import ams_command

        assert not hasattr(ams_command, "_ACK_SUCCESS_RESULTS")
        for result in ("success", "SUCCESS", "ok", "fail", None):
            observation = Observation()
            observation.fold_ack(_ack(result))
            assert observation.acked is _ack(result).succeeded

    def test_an_ack_once_seen_stays_seen(self) -> None:
        """A later ``None`` (the ACK rolled out of the client's bounded log) changes nothing."""
        observation = Observation()

        observation.fold_ack(_ack("success"))
        observation.fold_ack(None)

        assert observation.acked is True


# The three rows ``held`` belongs to, with a target that differs from the entry feeder.
_HELD_ROWS: list[tuple[Command, Posture, int | None]] = [
    ("unload", "mid_change_loaded", None),
    ("load", "mid_change_loaded", 2),
    ("load", "mid_change_empty", 2),
]


class TestHeldBehindTheChange:
    """``held`` — acknowledged + unmoved in a mid-change AMS. 012-H2S 2026-09-23: six
    commands sent into the loaded wedge each echoed ``success`` and moved nothing, then
    ran in order after the terminal."""

    @pytest.mark.parametrize("command, entry_posture, target", _HELD_ROWS)
    def test_a_success_ack_and_nothing_moved_is_held_at_the_grace(
        self, command: Command, entry_posture: Posture, target: int | None
    ) -> None:
        entry = _ENTRY[entry_posture]

        assert _run(command, target, entry, _held(entry, 1.0, UNLOAD_GRACE_S - 0.1), ack=_ack("success")) == (
            None,
            None,
        )
        assert _run(command, target, entry, _held(entry, 1.0, UNLOAD_GRACE_S), ack=_ack("success")) == (
            "held",
            UNLOAD_GRACE_S,
        )

    @pytest.mark.parametrize("command, entry_posture, target", _HELD_ROWS)
    def test_no_ack_is_no_movement_at_the_deadline(
        self, command: Command, entry_posture: Posture, target: int | None
    ) -> None:
        entry = _ENTRY[entry_posture]

        assert _run(command, target, entry, _held(entry, 1.0, UNLOAD_GRACE_S, 50.0, _DRIVER_DEADLINE_S)) == (
            "no_movement",
            _DRIVER_DEADLINE_S,
        )

    @pytest.mark.parametrize("result", ["fail", None])
    @pytest.mark.parametrize("command, entry_posture, target", _HELD_ROWS)
    def test_a_failure_ack_is_no_movement_at_the_deadline(
        self, command: Command, entry_posture: Posture, target: int | None, result: str | None
    ) -> None:
        entry = _ENTRY[entry_posture]
        polls = _held(entry, 1.0, UNLOAD_GRACE_S, 50.0, _DRIVER_DEADLINE_S)

        assert _run(command, target, entry, polls, ack=_ack(result)) == ("no_movement", _DRIVER_DEADLINE_S)

    @pytest.mark.parametrize("command, entry_posture, target", _HELD_ROWS)
    def test_an_ack_landing_after_the_grace_is_held_at_the_first_poll_that_reads_it(
        self, command: Command, entry_posture: Posture, target: int | None
    ) -> None:
        entry = _ENTRY[entry_posture]
        polls = _held(entry, 1.0, UNLOAD_GRACE_S, 29.0, 30.0, _DRIVER_DEADLINE_S)

        assert _run(command, target, entry, polls, ack=_ack("success"), ack_from_s=30.0) == ("held", 30.0)

    @pytest.mark.parametrize("command, entry_posture, target", _HELD_ROWS)
    def test_a_movement_is_never_held(self, command: Command, entry_posture: Posture, target: int | None) -> None:
        entry = _ENTRY[entry_posture]
        stirred = replace(entry, ams_status_sub=6)

        assert _run(command, target, entry, _held(stirred, 1.0, UNLOAD_GRACE_S, 50.0), ack=_ack("success")) == (
            None,
            None,
        )
        assert _run(command, target, entry, _held(stirred, 1.0, _DRIVER_DEADLINE_S), ack=_ack("success")) == (
            "acted",
            _DRIVER_DEADLINE_S,
        )

    @pytest.mark.parametrize("command, entry_posture, target", _HELD_ROWS)
    def test_the_operator_window_answers_held_at_its_end(
        self, command: Command, entry_posture: Posture, target: int | None
    ) -> None:
        """A window shorter than the grace (the operator's) reads the held rule at its own
        deadline — the ``_unload_mid_change_empty`` pattern — so an acknowledged click
        into a wedge is told ``held``, not ``no_movement``."""
        entry = _ENTRY[entry_posture]
        polls = _held(entry, 1.0, OPERATOR_ACK_S)

        assert _run(command, target, entry, polls, deadline_s=OPERATOR_ACK_S, ack=_ack("success")) == (
            "held",
            OPERATOR_ACK_S,
        )
        assert _run(command, target, entry, polls, deadline_s=OPERATOR_ACK_S) == ("no_movement", OPERATOR_ACK_S)

    def test_an_unload_that_empties_the_feeder_after_the_grace_is_still_complete(self) -> None:
        """``complete`` keeps its own rule inside the step timeout: the feeder leaves for
        255 at 10 s and the empty reading holds for the grace after its edge."""
        entry = _ENTRY["mid_change_loaded"]
        emptied = _snap(main=1, sub=5, tray_now=255)
        polls = [(1.0, entry), (10.0, emptied), (24.9, emptied)]

        assert _run("unload", None, entry, polls, ack=_ack("success")) == (None, None)
        assert _run("unload", None, entry, [*polls, (25.0, emptied)], ack=_ack("success")) == ("complete", 25.0)

    @pytest.mark.parametrize("entry_posture", ["mid_change_loaded", "mid_change_empty"])
    def test_a_load_that_reaches_its_target_after_the_grace_is_still_complete(self, entry_posture: Posture) -> None:
        entry = _ENTRY[entry_posture]
        polls = [(1.0, entry), (10.0, replace(entry, tray_tar=2)), (40.0, _with(entry, tray_now=2, tray_tar=2))]

        assert _run("load", 2, entry, polls, ack=_ack("success")) == ("complete", 40.0)

    def test_an_unload_with_nothing_loaded_stays_undecidable(self) -> None:
        """``(unload, mid_change_empty)``: nothing physical can answer — an ACK does not
        make a no-op a hold."""
        entry = _ENTRY["mid_change_empty"]

        assert _run("unload", None, entry, _held(entry, 1.0, UNLOAD_GRACE_S), ack=_ack("success")) == (
            "undecidable",
            UNLOAD_GRACE_S,
        )

    def test_held_is_reachable_from_exactly_the_rows_whose_feeder_can_move(self) -> None:
        """The table-level pin: every row, fed an acknowledged and unmoved window — the
        mid-change rows that can move, and outside a change the loaded unload and the load
        into an empty path (:class:`TestHeldOutsideAChange`)."""
        held_rows = {
            (command, entry_posture)
            for command, entry_posture in ams_command._ROWS
            if _run(
                command,
                5,
                _ENTRY[entry_posture],
                _held(_ENTRY[entry_posture], 1.0, UNLOAD_GRACE_S, _DRIVER_DEADLINE_S),
                ack=_ack("success"),
            )[0]
            == "held"
        }

        assert held_rows == {(command, entry_posture) for command, entry_posture, _ in _HELD_ROWS} | {
            (command, entry_posture) for command, entry_posture, _ in _HELD_AT_THE_BOUND_ROWS
        }


# Outside a change, the rows whose entry feeder could move: an unload with filament loaded,
# a load into an empty path.
_HELD_AT_THE_BOUND_ROWS: list[tuple[Command, Posture, int | None]] = [
    ("unload", "idle_loaded", None),
    ("unload", "assist_loaded", None),
    ("unload", "other_loaded", None),
    ("load", "idle_empty", 2),
    ("load", "assist_empty", 2),
    ("load", "other_empty", 2),
]


class TestHeldOutsideAChange:
    """``held`` outside a filament change, at the step bound (K5, 2026-10-10). 011-H2S
    2026-10-09 04:31:27 and 014-H2S 2026-10-10 02:45:58: a pull-back sent with filament
    loaded into an AMS in assist (after the ladder's ``ams_control resume`` released the
    change, the print PAUSED) was echoed ``result=success`` and moved nothing for the
    driver's 90 s, which answered ``no_movement`` and gave up — and the AMS ran it ON ITS
    OWN at 04:36:01 and 02:50:38, emptying the toolhead under the paused print."""

    def test_the_measured_cell_an_acknowledged_unmoved_pull_back_in_assist_is_held_at_the_bound(self) -> None:
        entry = _ENTRY["assist_loaded"]

        assert _run("unload", None, entry, _held(entry, 1.0, 50.0, _DRIVER_DEADLINE_S - 0.1), ack=_ack("success")) == (
            None,
            None,
        )
        assert _run("unload", None, entry, _held(entry, 1.0, 50.0, _DRIVER_DEADLINE_S), ack=_ack("success")) == (
            "held",
            _DRIVER_DEADLINE_S,
        )

    @pytest.mark.parametrize("command, entry_posture, target", _HELD_AT_THE_BOUND_ROWS)
    def test_held_only_at_the_bound_never_early_at_the_grace(
        self, command: Command, entry_posture: Posture, target: int | None
    ) -> None:
        """Outside a change a command normally runs at once, so the grace is no evidence of
        holding: the answer waits for the whole step window."""
        entry = _ENTRY[entry_posture]
        polls = _held(entry, 1.0, UNLOAD_GRACE_S, 50.0)

        assert _run(command, target, entry, polls, ack=_ack("success")) == (None, None)
        assert _run(command, target, entry, [*polls, (_DRIVER_DEADLINE_S, entry)], ack=_ack("success")) == (
            "held",
            _DRIVER_DEADLINE_S,
        )

    @pytest.mark.parametrize("result", ["fail", None])
    @pytest.mark.parametrize("command, entry_posture, target", _HELD_AT_THE_BOUND_ROWS)
    def test_no_ack_or_a_failure_ack_is_no_movement(
        self, command: Command, entry_posture: Posture, target: int | None, result: str | None
    ) -> None:
        entry = _ENTRY[entry_posture]
        polls = _held(entry, 1.0, _DRIVER_DEADLINE_S)

        assert _run(command, target, entry, polls) == ("no_movement", _DRIVER_DEADLINE_S)
        assert _run(command, target, entry, polls, ack=_ack(result)) == ("no_movement", _DRIVER_DEADLINE_S)

    @pytest.mark.parametrize("command, entry_posture, target", _HELD_AT_THE_BOUND_ROWS)
    def test_a_movement_is_acted_never_held(self, command: Command, entry_posture: Posture, target: int | None) -> None:
        entry = _ENTRY[entry_posture]
        stirred = replace(entry, ams_status_sub=6)

        assert _run(command, target, entry, _held(stirred, 1.0, _DRIVER_DEADLINE_S), ack=_ack("success")) == (
            "acted",
            _DRIVER_DEADLINE_S,
        )

    @pytest.mark.parametrize("entry_posture", ["assist_empty", "other_empty"])
    def test_an_unload_with_nothing_loaded_is_not_held(self, entry_posture: Posture) -> None:
        """Nothing at the feeder to pull back: an ACK does not make a no-op a hold."""
        entry = _ENTRY[entry_posture]

        assert _run("unload", None, entry, _held(entry, 1.0, _DRIVER_DEADLINE_S), ack=_ack("success")) == (
            "no_movement",
            _DRIVER_DEADLINE_S,
        )

    def test_an_idle_unload_with_nothing_loaded_is_still_the_dwells_completion(self) -> None:
        entry = _ENTRY["idle_empty"]

        assert _run("unload", None, entry, _held(entry, 1.0, 1.0 + UNLOAD_GRACE_S), ack=_ack("success")) == (
            "complete",
            1.0 + UNLOAD_GRACE_S,
        )

    @pytest.mark.parametrize("entry_posture", _OUTSIDE_LOADED)
    def test_a_load_into_a_loaded_path_is_not_held(self, entry_posture: Posture) -> None:
        """Unmeasured, and never the farm's own send (the unload goes first, invariant 8):
        the deadline rule answers it."""
        entry = _ENTRY[entry_posture]

        assert _run("load", 5, entry, _held(entry, 1.0, _DRIVER_DEADLINE_S), ack=_ack("success")) == (
            "no_movement",
            _DRIVER_DEADLINE_S,
        )

    def test_the_operator_window_answers_held_at_its_end(self) -> None:
        """An operator's Unload click with filament loaded, acknowledged and not run within
        the 5 s window, is told it is held — never "did not move"."""
        entry = _ENTRY["idle_loaded"]
        polls = _held(entry, 1.0, OPERATOR_ACK_S)

        assert _run("unload", None, entry, polls, deadline_s=OPERATOR_ACK_S, ack=_ack("success")) == (
            "held",
            OPERATOR_ACK_S,
        )


# --- did a recorded motion command run ----------------------------------------------------


def _command_step(command: str, *, target: int | None = None, feeder: str | None = None) -> StepEntry:
    """A ``command`` step as the step ledger records it (``printer_incident_step``)."""
    return StepEntry(kind=STEP_KIND_COMMAND, name=command, target=target, feeder=feeder)


def _live(tray_now: object) -> ToolheadFeed:
    return toolhead_feed(SimpleNamespace(tray_now=tray_now, h2d_extruder_snow={}, active_extruder=0))


class TestRan:
    """``ran(step, live)`` — did a RECORDED motion command run, read off the step's own
    recorded columns and the live K1 reading. Pure and DB-free: its readers are the driver's
    round top after a restart (never send behind a command that may still run) and the
    per-push detector of a farm command that ran after the hand-over (011/014-H2S: an
    accepted pull-back ran on its own ~4.5 min after the send)."""

    @pytest.mark.parametrize("recorded", ["jammed", "other"])
    @pytest.mark.parametrize(
        "tray_now, expected",
        [
            pytest.param(255, True, id="the_toolhead_now_reads_empty"),
            pytest.param(3, False, id="still_fed"),
            pytest.param(254, False, id="the_external_spool_feeds"),
            pytest.param(None, None, id="no_reading"),
        ],
    )
    def test_an_unload_sent_loaded_ran_when_the_active_extruder_reads_empty(
        self, recorded: str, tray_now: object, expected: bool | None
    ) -> None:
        assert ran(_command_step("unload", feeder=recorded), _live(tray_now)) is expected

    @pytest.mark.parametrize("recorded", ["empty", "external", "unknown", None])
    @pytest.mark.parametrize("tray_now", [255, 3, 254])
    def test_an_unload_sent_with_no_feeder_loaded_is_undecidable(self, recorded: str | None, tray_now: int) -> None:
        """Nothing was at the feeder for it to pull back (or nobody read what was): no live
        reading is its answer."""
        assert ran(_command_step("unload", feeder=recorded), _live(tray_now)) is None

    @pytest.mark.parametrize(
        "tray_now, expected",
        [
            pytest.param(2, True, id="the_target_feeds"),
            pytest.param(3, False, id="another_tray_feeds"),
            pytest.param(255, False, id="nothing_fed"),
            pytest.param(254, False, id="the_external_spool_feeds"),
            pytest.param("x", None, id="no_reading"),
        ],
    )
    def test_a_load_ran_when_the_active_feeder_is_its_target(self, tray_now: object, expected: bool | None) -> None:
        assert ran(_command_step("load", target=2, feeder="empty"), _live(tray_now)) is expected

    @pytest.mark.parametrize("target", [None, 254, 255])
    def test_a_load_whose_target_is_no_ams_feeder_is_undecidable(self, target: int | None) -> None:
        assert ran(_command_step("load", target=target), _live(254)) is None

    def test_a_dual_nozzle_reads_the_active_extruder(self) -> None:
        """The right nozzle still holds slot 5; the left, active one is empty — the pull-back
        from the active nozzle ran."""
        live = toolhead_feed(SimpleNamespace(tray_now=5, h2d_extruder_snow={0: 5, 1: 255}, active_extruder=1))

        assert ran(_command_step("unload", feeder="jammed"), live) is True
        assert ran(_command_step("load", target=5), live) is False

    @pytest.mark.parametrize(
        "step",
        [
            pytest.param(StepEntry(kind=STEP_KIND_LEVER, name="resume"), id="a_lever"),
            pytest.param(StepEntry(kind=STEP_KIND_COMMAND, name="purge"), id="an_unknown_command"),
        ],
    )
    def test_only_a_motion_command_can_be_asked(self, step: StepEntry) -> None:
        """Motion-only: a lever's effect is the release reader's, and a token the module
        cannot name is ledger drift, never a skipped row."""
        with pytest.raises(LookupError):
            ran(step, _live(255))

    def test_the_loaded_kinds_are_the_drivers_own_feeder_vocabulary(self) -> None:
        """``ran`` reads the feeder-position kind the recovery driver RECORDS at the send
        (``spool_recovery.FeederKind``): both kinds that name a real feeder at the extruder
        count as loaded, and every kind it reads is one the driver can write."""
        from backend.app.services.spool_recovery import FeederKind

        assert {"jammed", "other"} == ams_command._RECORDED_LOADED_FEEDERS
        assert set(get_args(FeederKind)) >= ams_command._RECORDED_LOADED_FEEDERS


# --- the verbs -----------------------------------------------------------------------------


class _FakeClient:
    """The two motion publishers plus the ACK lane's two reads, recording call order into
    a shared log.

    Every publish that goes out is numbered like the real client's (``"1"``, ``"2"``, …,
    read back through ``last_sent_sequence_id``); ``echo`` makes the fake firmware ACK
    each send with that ``result`` under the send's own id, as the report-topic echo does.
    """

    def __init__(
        self,
        log: list[str],
        *,
        sent: bool = True,
        on_publish: Callable[[int | None], None] | None = None,
        echo: str | None = None,
    ) -> None:
        self.log = log
        self.sent = sent
        self.on_publish = on_publish
        self.echo = echo
        self.loads: list[int] = []
        self.unloads = 0
        self.acks: list[CommandAck] = []
        self._seq = 0
        self._last_sent: str | None = None

    def _went_out(self, tray: int | None) -> None:
        self._seq += 1
        self._last_sent = str(self._seq)
        if self.echo is not None:
            self.acks.append(_ack(self.echo, seq=self._last_sent))
        if self.on_publish is not None:
            self.on_publish(tray)

    def ams_load_filament(self, tray_id: int) -> bool:
        self.log.append(f"publish_load:{tray_id}")
        self.loads.append(tray_id)
        if self.sent:
            self._went_out(tray_id)
        return self.sent

    def ams_unload_filament(self) -> bool:
        self.log.append("publish_unload")
        self.unloads += 1
        if self.sent:
            self._went_out(None)
        return self.sent

    def last_sent_sequence_id(self, command: str) -> str | None:
        return self._last_sent if command == "ams_change_filament" else None

    def ack_for(self, command: str, sequence_id: str) -> CommandAck | None:
        return next((a for a in reversed(self.acks) if (a.command, a.sequence_id) == (command, sequence_id)), None)


class _FakeManager:
    """The three ``printer_manager`` reads ``ams_command`` makes."""

    def __init__(
        self,
        *,
        client: _FakeClient | None,
        state: PrinterState | None,
        name: str | None = "012-H2S",
        on_status: Callable[[], None] | None = None,
    ) -> None:
        self.client = client
        self.state = state
        self.info = SimpleNamespace(name=name) if name is not None else None
        self.on_status = on_status

    def get_client(self, printer_id: int) -> _FakeClient | None:
        return self.client

    def get_status(self, printer_id: int) -> PrinterState | None:
        if self.on_status is not None:
            self.on_status()
        return self.state

    def get_printer(self, printer_id: int) -> SimpleNamespace | None:
        return self.info


@pytest.fixture
def call_log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Order of the pre-send mark and the publish, one shared log."""
    log: list[str] = []
    monkeypatch.setattr(
        spool_respool, "note_commanded_load", lambda printer_id, tray: log.append(f"mark:{printer_id}:{tray}")
    )
    return log


def _install(monkeypatch: pytest.MonkeyPatch, manager: _FakeManager) -> _FakeManager:
    monkeypatch.setattr(ams_command, "printer_manager", manager)
    return manager


def _state(**fields: object) -> PrinterState:
    base: dict[str, object] = {"connected": True, "connection_epoch": 1, "state": "IDLE"}
    base.update(fields)
    return PrinterState(**base)  # type: ignore[arg-type]


class TestVerbRefusals:
    @pytest.mark.parametrize("verb", ["load", "unload"])
    def test_no_client_is_not_connected(self, monkeypatch: pytest.MonkeyPatch, call_log: list[str], verb: str) -> None:
        _install(monkeypatch, _FakeManager(client=None, state=None))

        sent = (
            ams_command.load(_PID, 5, actor="operator")
            if verb == "load"
            else ams_command.unload(_PID, actor="operator")
        )

        assert sent == Refusal("not_connected", "Printer not connected")
        assert call_log == []

    @pytest.mark.parametrize(
        "hms, where",
        [
            pytest.param([_SLOT3_DEMAND, _BARE_8011], "AMS A slot 3", id="slot_attributed_demand"),
            pytest.param([_BARE_8011], "the slot that ran out", id="slot_agnostic_runout"),
        ],
    )
    def test_a_runout_hold_refuses_a_load_with_todays_copy(
        self, monkeypatch: pytest.MonkeyPatch, call_log: list[str], hms: list[SimpleNamespace], where: str
    ) -> None:
        client = _FakeClient(call_log)
        _install(monkeypatch, _FakeManager(client=client, state=_state(state="PAUSE", hms_errors=hms), name="006-H2S"))

        sent = ams_command.load(_PID, 1, actor="operator")

        assert sent == Refusal(
            "runout_hold",
            f"006-H2S is PAUSEd waiting for a filament refill in {where}. The AMS executes no load in this "
            "state — the request would latch in the firmware and resurface at the resume as a bogus demand "
            f"for this slot (006-H2S 2026-07-26). Insert filament into {where} and the print resumes from there.",
        )
        assert call_log == []  # no mark, no publish — the latch is never armed

    def test_a_runout_hold_does_not_refuse_an_unload(
        self, monkeypatch: pytest.MonkeyPatch, call_log: list[str]
    ) -> None:
        client = _FakeClient(call_log)
        _install(monkeypatch, _FakeManager(client=client, state=_state(state="PAUSE", hms_errors=[_BARE_8011])))

        assert isinstance(ams_command.unload(_PID, actor="operator"), Sent)
        assert client.unloads == 1

    @pytest.mark.parametrize("verb", ["load", "unload"])
    def test_a_mid_change_ams_is_not_refused(
        self, monkeypatch: pytest.MonkeyPatch, call_log: list[str], verb: str
    ) -> None:
        """The retracted premise: no posture refuses a command any more."""
        client = _FakeClient(call_log)
        _install(
            monkeypatch,
            _FakeManager(client=client, state=_state(state="PAUSE", ams_status_main=1, ams_status_sub=5, tray_now=3)),
        )

        sent = (
            ams_command.load(_PID, 2, actor="operator")
            if verb == "load"
            else ams_command.unload(_PID, actor="operator")
        )

        assert isinstance(sent, Sent)
        assert posture(sent.entry) == "mid_change_loaded"
        assert len(client.loads) + client.unloads == 1

    @pytest.mark.parametrize("verb", ["load", "unload"])
    def test_a_publish_that_did_not_go_out_is_not_connected(
        self, monkeypatch: pytest.MonkeyPatch, call_log: list[str], caplog: pytest.LogCaptureFixture, verb: str
    ) -> None:
        caplog.set_level(logging.INFO, logger=_LOGGER)
        _install(monkeypatch, _FakeManager(client=_FakeClient(call_log, sent=False), state=_state()))

        sent = (
            ams_command.load(_PID, 5, actor="operator")
            if verb == "load"
            else ams_command.unload(_PID, actor="operator")
        )

        assert isinstance(sent, Refusal) and sent.reason == "not_connected"
        assert "not connected" in sent.detail.lower()
        assert operator_commanded_since(_PID, 0.0) is False  # an unsent click attributes nothing
        assert [r.levelno for r in caplog.records if "[ams-command]" in r.getMessage()] == [logging.WARNING]


class TestVerbSend:
    def test_the_mark_and_the_snapshot_precede_the_publish(
        self, monkeypatch: pytest.MonkeyPatch, call_log: list[str]
    ) -> None:
        state = _state(tray_now=255, ams_status_main=0)

        def lands(tray: int | None) -> None:
            state.tray_now = tray if tray is not None else 255
            state.ams_status_main = 1

        _install(monkeypatch, _FakeManager(client=_FakeClient(call_log, on_publish=lands), state=state))

        sent = ams_command.load(_PID, 5, actor="driver")

        assert call_log == [f"mark:{_PID}:5", "publish_load:5"]
        assert isinstance(sent, Sent)
        assert (sent.entry.tray_now, sent.entry.ams_status_main) == (255, 0)  # read BEFORE the publish moved it

    def test_an_unload_snapshot_precedes_the_publish(
        self, monkeypatch: pytest.MonkeyPatch, call_log: list[str]
    ) -> None:
        state = _state(tray_now=3, ams_status_main=0)

        def lands(tray: int | None) -> None:
            state.tray_now = 255

        _install(monkeypatch, _FakeManager(client=_FakeClient(call_log, on_publish=lands), state=state))

        sent = ams_command.unload(_PID, actor="driver")

        assert call_log == ["publish_unload"]  # an unload carries no load mark
        assert isinstance(sent, Sent) and sent.entry.tray_now == 3

    def test_each_send_carries_its_own_sequence_id(self, monkeypatch: pytest.MonkeyPatch, call_log: list[str]) -> None:
        """The id the client published under, read right after the publish — the key the
        firmware's ACK is correlated by."""
        state = _state(tray_now=3, ams_status_main=0)
        _install(monkeypatch, _FakeManager(client=_FakeClient(call_log), state=state))

        first = ams_command.load(_PID, 2, actor="driver")
        second = ams_command.unload(_PID, actor="driver")

        assert first == Sent(entry=first.entry, sequence_id="1")
        assert second == Sent(entry=second.entry, sequence_id="2")

    def test_the_send_line_carries_the_entry_snapshot(
        self, monkeypatch: pytest.MonkeyPatch, call_log: list[str], caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger=_LOGGER)
        state = _state(tray_now=3, ams_status_main=1, ams_status_sub=5, tray_tar=3, connection_epoch=7)
        _install(monkeypatch, _FakeManager(client=_FakeClient(call_log), state=state))

        ams_command.load(_PID, 2, actor="driver")

        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[ams-command]")]
        assert lines == [
            f"[ams-command] actor=driver command=load printer={_PID} target=2 seq=1 posture=mid_change_loaded "
            "entry tray_now=3 ams_status=1/5 tray_tar=3 epoch=7"
        ]


class TestAckOf:
    """``ack_of`` — the ONE correlation of a send to the firmware's ACK for it."""

    def _sent(self, seq: str | None) -> Sent:
        return Sent(entry=_ENTRY["mid_change_loaded"], sequence_id=seq)

    def test_reads_the_ack_for_the_send_s_own_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient([])
        client.acks = [_ack("fail", seq="6"), _ack("success", seq="7"), _ack("fail", seq="8")]
        _install(monkeypatch, _FakeManager(client=client, state=None))

        assert ack_of(_PID, self._sent("7")) == _ack("success", seq="7")

    def test_an_ack_for_another_command_is_not_this_send_s(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The ACK is keyed by ``(command, id)``: an ACK another command carries under the
        same id is not this send's."""
        client = _FakeClient([])
        client.acks = [_ack("success", seq="7", command="ams_control")]
        _install(monkeypatch, _FakeManager(client=client, state=None))

        assert ack_of(_PID, self._sent("7")) is None

    def test_no_client_or_no_id_is_no_ack(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient([])
        client.acks = [_ack("success", seq="7")]
        _install(monkeypatch, _FakeManager(client=None, state=None))
        assert ack_of(_PID, self._sent("7")) is None

        _install(monkeypatch, _FakeManager(client=client, state=None))
        assert ack_of(_PID, self._sent(None)) is None


class TestOperatorAttribution:
    @pytest.mark.parametrize("verb", ["load", "unload"])
    def test_only_an_operator_command_is_stamped(
        self, monkeypatch: pytest.MonkeyPatch, call_log: list[str], verb: str
    ) -> None:
        _install(monkeypatch, _FakeManager(client=_FakeClient(call_log), state=_state()))

        if verb == "load":
            ams_command.load(_PID, 5, actor="driver")
        else:
            ams_command.unload(_PID, actor="driver")
        assert operator_commanded_since(_PID, 0.0) is False

        if verb == "load":
            ams_command.load(_PID, 5, actor="operator")
        else:
            ams_command.unload(_PID, actor="operator")
        assert operator_commanded_since(_PID, 0.0) is True

    def test_since_is_a_time_bound(
        self, monkeypatch: pytest.MonkeyPatch, call_log: list[str], clock: FakeClock
    ) -> None:
        monkeypatch.setattr(ams_command, "_monotonic", clock)
        _install(monkeypatch, _FakeManager(client=_FakeClient(call_log), state=_state()))

        ams_command.unload(_PID, actor="operator")  # stamped at t=1000

        assert operator_commanded_since(_PID, clock.t) is True
        assert operator_commanded_since(_PID, clock.t + 0.001) is False
        assert operator_commanded_since(_PID + 1, 0.0) is False  # per printer

    def test_a_stamp_from_an_older_session_is_not_since_anything(
        self, monkeypatch: pytest.MonkeyPatch, call_log: list[str]
    ) -> None:
        state = _state(connection_epoch=1)
        _install(monkeypatch, _FakeManager(client=_FakeClient(call_log), state=state))
        ams_command.unload(_PID, actor="operator")
        assert operator_commanded_since(_PID, 0.0) is True

        state.connection_epoch = 2  # the printer reconnected

        assert operator_commanded_since(_PID, 0.0) is False


# --- the operator facade -------------------------------------------------------------------


@pytest.fixture
def short_ack(monkeypatch: pytest.MonkeyPatch) -> float:
    """The operator window, shortened so a "nothing happened" case does not wait 5 s."""
    monkeypatch.setattr(ams_command, "OPERATOR_ACK_S", 0.05)
    return 0.05


class TestCommandForOperator:
    async def test_no_client_maps_to_refused_not_connected(
        self, monkeypatch: pytest.MonkeyPatch, call_log: list[str]
    ) -> None:
        _install(monkeypatch, _FakeManager(client=None, state=None))

        assert await command_for_operator(_PID, "unload") == AmsCommandResult(
            "refused_not_connected", "Printer not connected", None
        )

    async def test_a_runout_hold_maps_to_refused_runout_hold_with_its_sentence(
        self, monkeypatch: pytest.MonkeyPatch, call_log: list[str]
    ) -> None:
        _install(
            monkeypatch,
            _FakeManager(client=_FakeClient(call_log), state=_state(state="PAUSE", hms_errors=[_SLOT3_DEMAND])),
        )

        result = await command_for_operator(_PID, "load", 1)

        assert result.outcome == "refused_runout_hold"
        assert "AMS A slot 3" in result.message and "latch" in result.message
        assert result.family is None  # nothing was sent, so no posture was read

    async def test_a_load_that_reaches_its_tray_is_complete(
        self, monkeypatch: pytest.MonkeyPatch, call_log: list[str], short_ack: float
    ) -> None:
        state = _state(tray_now=255)

        def lands(tray: int | None) -> None:
            state.tray_now = tray if tray is not None else 255

        _install(monkeypatch, _FakeManager(client=_FakeClient(call_log, on_publish=lands), state=state))

        assert await command_for_operator(_PID, "load", 5) == AmsCommandResult(
            "complete", "Loading filament from AMS 1 slot 2", "outside_change"
        )

    @pytest.mark.parametrize(
        "tray_id, label", [(254, "external spool"), (255, "Ext-R"), (0, "AMS 0 slot 1")], ids=["ext_l", "ext_r", "slot"]
    )
    async def test_a_load_that_moved_is_acted_with_todays_sentence(
        self, monkeypatch: pytest.MonkeyPatch, call_log: list[str], short_ack: float, tray_id: int, label: str
    ) -> None:
        state = _state(tray_now=255, ams_status_main=0)

        def stirs(tray: int | None) -> None:
            state.ams_status_main = 1

        _install(monkeypatch, _FakeManager(client=_FakeClient(call_log, on_publish=stirs), state=state))

        assert await command_for_operator(_PID, "load", tray_id) == AmsCommandResult(
            "acted", f"Loading filament from {label}", "outside_change"
        )

    @pytest.mark.parametrize(
        "command, tray_id, message",
        [("load", 3, "Load sent. AMS did not move."), ("unload", None, "Unload sent. AMS did not move.")],
    )
    async def test_a_wire_that_never_moves_is_no_movement(
        self,
        monkeypatch: pytest.MonkeyPatch,
        call_log: list[str],
        short_ack: float,
        command: Command,
        tray_id: int | None,
        message: str,
    ) -> None:
        _install(monkeypatch, _FakeManager(client=_FakeClient(call_log), state=_state(tray_now=1, ams_status_main=0)))

        assert await command_for_operator(_PID, command, tray_id) == AmsCommandResult(
            "no_movement", message, "outside_change"
        )

    @pytest.mark.parametrize(
        "command, tray_id, message",
        [
            ("load", 2, "Load accepted. Held behind the paused print's filament change."),
            ("unload", None, "Unload accepted. Held behind the paused print's filament change."),
        ],
    )
    async def test_an_acknowledged_click_into_a_wedge_is_held(
        self,
        monkeypatch: pytest.MonkeyPatch,
        call_log: list[str],
        short_ack: float,
        command: Command,
        tray_id: int | None,
        message: str,
    ) -> None:
        """012-H2S 2026-09-23: the operator's clicks into the loaded wedge were each echoed
        ``success`` and moved nothing — the click is told so, not "did not move"."""
        state = _state(state="PAUSE", tray_now=3, ams_status_main=1, ams_status_sub=5)
        _install(monkeypatch, _FakeManager(client=_FakeClient(call_log, echo="success"), state=state))

        assert await command_for_operator(_PID, command, tray_id) == AmsCommandResult("held", message, "mid_change")

    @pytest.mark.parametrize(
        "command, tray_id, tray_now, message",
        [
            ("load", 2, 255, "Load accepted. Not run yet."),
            ("unload", None, 3, "Unload accepted. Not run yet. The AMS can run it later without another command."),
        ],
    )
    async def test_an_acknowledged_click_outside_a_change_is_held_with_its_own_sentence(
        self,
        monkeypatch: pytest.MonkeyPatch,
        call_log: list[str],
        short_ack: float,
        command: Command,
        tray_id: int | None,
        tray_now: int,
        message: str,
    ) -> None:
        """Outside a change there is no paused change to be held behind, so the in-change
        sentence would be false: the outside-change family carries its own (011/014-H2S: an
        accepted pull-back ran on its own ~4.5 min later)."""
        state = _state(tray_now=tray_now, ams_status_main=0)
        _install(monkeypatch, _FakeManager(client=_FakeClient(call_log, echo="success"), state=state))

        assert await command_for_operator(_PID, command, tray_id) == AmsCommandResult("held", message, "outside_change")

    @pytest.mark.parametrize("command, tray_id", [("load", 2), ("unload", None)])
    async def test_a_failure_ack_into_a_wedge_is_no_movement(
        self,
        monkeypatch: pytest.MonkeyPatch,
        call_log: list[str],
        short_ack: float,
        command: Command,
        tray_id: int | None,
    ) -> None:
        state = _state(state="PAUSE", tray_now=3, ams_status_main=1, ams_status_sub=5)
        _install(monkeypatch, _FakeManager(client=_FakeClient(call_log, echo="fail"), state=state))

        assert (await command_for_operator(_PID, command, tray_id)).outcome == "no_movement"

    async def test_an_unload_with_nothing_loaded_mid_change_is_undecidable(
        self, monkeypatch: pytest.MonkeyPatch, call_log: list[str], short_ack: float
    ) -> None:
        state = _state(state="PAUSE", tray_now=255, ams_status_main=1, ams_status_sub=5)
        _install(monkeypatch, _FakeManager(client=_FakeClient(call_log), state=state))

        assert await command_for_operator(_PID, "unload") == AmsCommandResult(
            "undecidable", "Unload sent. Nothing was loaded.", "mid_change"
        )

    async def test_an_unload_that_empties_a_mid_change_feeder_is_acted(
        self, monkeypatch: pytest.MonkeyPatch, call_log: list[str], short_ack: float
    ) -> None:
        state = _state(state="PAUSE", tray_now=3, ams_status_main=1, ams_status_sub=5)

        def empties(tray: int | None) -> None:
            state.tray_now = 255

        _install(monkeypatch, _FakeManager(client=_FakeClient(call_log, on_publish=empties), state=state))

        assert await command_for_operator(_PID, "unload") == AmsCommandResult(
            "acted", "Unloading filament", "mid_change"
        )

    @pytest.mark.parametrize("command, tray_id", [("load", 5), ("unload", None)])
    async def test_a_reconnect_during_the_command_is_session_changed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        call_log: list[str],
        short_ack: float,
        command: Command,
        tray_id: int | None,
    ) -> None:
        """The operator's window is short (:data:`OPERATOR_ACK_S`): a new session whose
        first report has not landed by its end reads ``session_changed`` — the toast copy
        stands. (Since F4 the classifier waits the cache out instead of answering at once,
        so the window is shortened here like every "nothing happened" case.)"""
        state = _state(connection_epoch=3)

        def reconnects(tray: int | None) -> None:
            state.connection_epoch = 4
            state.report_epoch = None  # _on_connect: the new session's report has not landed

        _install(monkeypatch, _FakeManager(client=_FakeClient(call_log, on_publish=reconnects), state=state))

        assert await command_for_operator(_PID, command, tray_id) == AmsCommandResult(
            "session_changed", "Printer reconnected during the command. Check the AMS.", "outside_change"
        )

    def test_the_operator_window_is_five_seconds(self) -> None:
        assert ams_command.OPERATOR_ACK_S == 5.0

    async def test_a_reconnect_whose_first_report_lands_inside_the_window_answers_from_motion(
        self, monkeypatch: pytest.MonkeyPatch, call_log: list[str], short_ack: float
    ) -> None:
        """The session changed under the click, and the new session's first FRESH report —
        inside the window — shows the load on its tray: ``complete``, not a reconnect toast."""
        state = _state(connection_epoch=3)

        def reconnects_loaded(tray: int | None) -> None:
            state.connection_epoch = 4
            state.tray_now = tray
            state.report_epoch = 4  # the new session's first report, applied

        _install(monkeypatch, _FakeManager(client=_FakeClient(call_log, on_publish=reconnects_loaded), state=state))

        assert await command_for_operator(_PID, "load", 5) == AmsCommandResult(
            "complete", "Loading filament from AMS 1 slot 2", "outside_change"
        )

    async def test_a_load_without_a_tray_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="needs a tray_id"):
            await command_for_operator(_PID, "load")

    def test_the_copy_covers_exactly_the_answers_each_command_can_get_in_each_family(self) -> None:
        """Keyed by posture family: only an unload into a mid-change AMS with nothing loaded
        is ``undecidable``, so that cell exists in the mid-change family alone."""
        reachable = {
            (command, answer, family)
            for command in get_args(Command)
            for answer in get_args(Answer)
            for family in get_args(PostureFamily)
            if answer != "undecidable" or (command, family) == ("unload", "mid_change")
        }
        assert set(ams_command._ANSWER_COPY) == reachable

    def test_held_has_a_sentence_of_its_own_in_each_family(self) -> None:
        for command in get_args(Command):
            assert (
                ams_command._ANSWER_COPY[(command, "held", "mid_change")]
                != ams_command._ANSWER_COPY[(command, "held", "outside_change")]
            )

    def test_the_operator_window_is_the_documented_five_seconds(self) -> None:
        assert OPERATOR_ACK_S == 5.0


class TestObserve:
    async def test_polls_until_the_grace_held_completion_and_logs_the_answer(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, clock: FakeClock
    ) -> None:
        """The 012-H2S posture over the driver's window: the feeder empties 1 s after
        the send and stays empty while the AMS stays in state 1 — complete once the
        empty reading has held for the grace."""
        caplog.set_level(logging.INFO, logger=_LOGGER)
        monkeypatch.setattr(ams_command, "_monotonic", clock)
        state = _state(state="PAUSE", tray_now=3, ams_status_main=1, ams_status_sub=5)
        entry = snapshot(state)

        def one_second_per_poll() -> None:
            clock.advance(1.0)
            state.tray_now = 255

        _install(monkeypatch, _FakeManager(client=None, state=state, on_status=one_second_per_poll))

        answer = await observe(
            _PID, "unload", None, Sent(entry=entry, sequence_id=None), timeout_s=_DRIVER_DEADLINE_S, poll_s=0.0
        )

        assert answer == "complete"
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[ams-command]")]
        assert lines == [
            f"[ams-command] actor=operator printer={_PID} command=unload target=None posture=mid_change_loaded "
            "answer=complete after 16.0s (tray_now 3→255 ams_status 1/5→1/5 tray_tar None→None)"
        ]

    async def test_answers_at_the_deadline_when_nothing_decides(
        self, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
    ) -> None:
        monkeypatch.setattr(ams_command, "_monotonic", clock)
        state = _state(tray_now=2, ams_status_main=0)
        entry = snapshot(state)
        _install(monkeypatch, _FakeManager(client=None, state=state, on_status=lambda: clock.advance(1.0)))

        assert await observe(_PID, "load", 5, Sent(entry=entry, sequence_id=None), timeout_s=3.0, poll_s=0.0) == (
            "no_movement"
        )
        assert clock.t == entry.taken_at + 3.0

    async def test_folds_the_ack_for_its_own_send_and_answers_held_at_the_grace(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, clock: FakeClock
    ) -> None:
        """The 012-H2S 2026-09-23 posture over the driver's window: the unload is echoed
        ``success`` and nothing moves — ``held`` at the grace, not a 90 s wait."""
        caplog.set_level(logging.INFO, logger=_LOGGER)
        monkeypatch.setattr(ams_command, "_monotonic", clock)
        state = _state(state="PAUSE", tray_now=3, ams_status_main=1, ams_status_sub=5)
        client = _FakeClient([])
        client.acks = [_ack("fail", seq="6"), _ack("success", seq="7")]
        entry = snapshot(state)
        _install(monkeypatch, _FakeManager(client=client, state=state, on_status=lambda: clock.advance(1.0)))

        answer = await observe(
            _PID, "unload", None, Sent(entry=entry, sequence_id="7"), timeout_s=_DRIVER_DEADLINE_S, poll_s=0.0
        )

        assert answer == "held"
        assert clock.t == entry.taken_at + UNLOAD_GRACE_S
        assert [r.getMessage() for r in caplog.records if "answer=" in r.getMessage()] == [
            f"[ams-command] actor=operator printer={_PID} command=unload target=None posture=mid_change_loaded "
            "answer=held after 15.0s (tray_now 3→3 ams_status 1/5→1/5 tray_tar None→None)"
        ]

    async def test_an_ack_for_an_older_send_is_not_this_one_s(
        self, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
    ) -> None:
        monkeypatch.setattr(ams_command, "_monotonic", clock)
        state = _state(state="PAUSE", tray_now=3, ams_status_main=1, ams_status_sub=5)
        client = _FakeClient([])
        client.acks = [_ack("success", seq="6")]
        entry = snapshot(state)
        _install(monkeypatch, _FakeManager(client=client, state=state, on_status=lambda: clock.advance(1.0)))

        assert await observe(_PID, "unload", None, Sent(entry=entry, sequence_id="7"), timeout_s=30.0, poll_s=0.0) == (
            "no_movement"
        )


# --- the single-caller pin -----------------------------------------------------------------

_MOTION_PUBLISHERS = frozenset({"ams_load_filament", "ams_unload_filament"})
_OWNER = Path("services") / "ams_command.py"


def _scan_motion_publishes(module: ParsedModule) -> list[tuple[str, int]]:
    """Every CALL of a client motion publisher, however the client was reached.

    AST, not grep, for the ``TestOperatorStopOwnership`` reason: the publishers are
    named in prose (and DEFINED in ``bambu_mqtt``) — only a ``Call`` counts.
    """
    hits: list[tuple[str, int]] = []
    for node in ast.walk(module.tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in _MOTION_PUBLISHERS:
            hits.append((name, node.lineno))
    return hits


class TestAmsMotionPublishOwnership:
    """Only ``ams_command`` publishes an AMS load or unload.

    A second caller would send a motion command nobody snapshots, classifies,
    attributes or marks — the measured answer this module exists for would silently
    stop covering it.
    """

    def test_only_ams_command_calls_the_client_motion_publishers(self, app_sources: ParsedTree) -> None:
        strays = [
            f"  - {module.rel_to_root.as_posix()}:{line} calls {name}()"
            for module in app_sources.modules()
            if module.rel_to_root != _OWNER
            for name, line in _scan_motion_publishes(module)
        ]
        if strays:
            pytest.fail(
                "Something outside services/ams_command.py publishes an AMS load/unload:\n"
                + "\n".join(strays)
                + "\n\nRoute it through ams_command.load / ams_command.unload — the verbs own the "
                "pre-send mark, the entry snapshot, the attribution stamp and the send line."
            )

    def test_the_owner_still_calls_both(self, app_sources: ParsedTree) -> None:
        """The liveness half: a pin whose owner stopped calling the publishers would pass
        on an empty set."""
        assert {name for name, _ in _scan_motion_publishes(app_sources.get(_OWNER))} == _MOTION_PUBLISHERS
