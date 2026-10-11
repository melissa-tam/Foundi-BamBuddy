"""``feed_state`` — THE owner of "is filament reaching the nozzle, and if not, is the firmware
about to put it back or is the print on air", driven through golden wire sequences.

Every sequence is a run of :class:`FeedFrame` s exactly as the MQTT client builds them, one per
status push, modelled on the wire measured on 2026-10-10 (H2S fleet): the drained slot's exist
bit clears while it still feeds; on some printers ``tray_now`` PRE-FLIPS to the backup ~1 s later
(the target can be an EMPTY slot); 2.5-4.7 min later ``tray_now`` reads 255 while RUNNING with
``ams_status_main`` not yet 1; HMS 0x00030001 arrives 1-3 s later and 0x00030002 ~15 s later with
the backup load landing; the layer does not advance during the change. Printing on air DOES
advance layers. The per-push air detector that acted on the FIRST frame of that sequence broke
into 3 of the 7 auto-switches that day (005/015/001-H2S) — none of these sequences may reach AIR.
"""

from __future__ import annotations

import pytest

from backend.app.services import feed_state, hms_errors, tray_fields
from backend.app.services.bambu_mqtt import HMSError, PrinterState
from backend.app.services.feed_state import (
    AutoSwitched,
    ChangeCause,
    FeedFrame,
    FeedPhase,
    FeedReading,
    FeedSignal,
    FeedTracker,
    JobBoundary,
)

_TOTAL = 167

IDLE = FeedPhase.IDLE
START_BLOCK = FeedPhase.START_BLOCK
END_RETRACT = FeedPhase.END_RETRACT
FED = FeedPhase.FED
TAIL = FeedPhase.TAIL
CHANGING = FeedPhase.CHANGING
RUNOUT_HELD = FeedPhase.RUNOUT_HELD
EMPTY_UNCONFIRMED = FeedPhase.EMPTY_UNCONFIRMED
AIR = FeedPhase.AIR
EMPTY_PAUSED = FeedPhase.EMPTY_PAUSED
UNKNOWN = FeedPhase.UNKNOWN


def _single(tray: int, extruder: int = 0) -> tray_fields.ToolheadFeed:
    """A single-nozzle toolhead: ``tray_now`` IS the one extruder's feeder."""
    reading = tray_fields.extruder_feed(tray)
    return tray_fields.ToolheadFeed(extruders=((extruder, reading),), active_extruder=extruder, active=reading)


def _dual(active: int, trays: dict[int, int]) -> tray_fields.ToolheadFeed:
    """A dual-nozzle (H2C) toolhead: each extruder's own ``snow`` feeder."""
    readings = {eid: tray_fields.extruder_feed(tray) for eid, tray in trays.items()}
    return tray_fields.ToolheadFeed(
        extruders=tuple(sorted(readings.items())), active_extruder=active, active=readings[active]
    )


def _presence(absent=(), unknown=()) -> dict[tuple[int, int], bool | None]:
    """Two AMS units, every tray seated unless named — keyed as ``tray_presence_map`` keys it."""
    out: dict[tuple[int, int], bool | None] = {}
    for tray in range(8):
        key = tray_fields.decode_global_tray(tray)
        out[key] = None if tray in unknown else tray not in absent  # type: ignore[index]
    return out


def frame(
    tray: int = 255,
    *,
    layer: int = 10,
    gcode: str = "RUNNING",
    total: int = _TOTAL,
    feed: tray_fields.ToolheadFeed | None = None,
    posture: bool = False,
    carried: bool = True,
    absent=(),
    unknown=(),
    demand: tuple[int, int] | None = None,
    held: bool = False,
    pull_back: bool = False,
    auto_switched: bool = False,
    job: bool = True,
    live: bool = True,
    epoch: int = 1,
    farm_acting: bool = False,
    boundary: JobBoundary = JobBoundary.NONE,
) -> FeedFrame:
    """ONE push. ``carried``: the push carried ``ams_status`` (a push raising the posture always
    did). ``held``: a slot-agnostic runout word (``07xx_8011``) stands; ``demand``: the slotted
    ``0x00020001`` demand stands."""
    return FeedFrame(
        live=live,
        epoch=epoch,
        gcode_state=gcode,
        feed=feed if feed is not None else _single(tray),
        posture_carried=carried or posture,
        posture=posture,
        presence=_presence(absent, unknown),
        runout_demand=demand,
        runout_demand_standing=held or demand is not None,
        pull_back=pull_back,
        auto_switched=auto_switched,
        layer=layer,
        total_layers=total,
        job_active=job,
        farm_acting=farm_acting,
        boundary=boundary,
    )


class _Printer:
    """One printer's tracker and every reading it produced, in push order."""

    def __init__(self) -> None:
        self.tracker = FeedTracker()
        self.readings: list[FeedReading] = []

    def push(self, tray: int = 255, **kw) -> FeedReading:
        reading = self.tracker.step(frame(tray, **kw))
        self.readings.append(reading)
        return reading

    def start(self, tray: int, **kw) -> None:
        """A job this tracker watched START: the start push (the new job; the session's first
        report, applied after the step, so not yet live), then the first live frame."""
        self.push(tray, layer=0, live=False, boundary=JobBoundary.NEW_JOB, **kw)
        self.push(tray, layer=0, **kw)

    @property
    def last(self) -> FeedReading:
        return self.readings[-1]

    @property
    def phase(self) -> FeedPhase:
        return self.last.phase

    def phases(self) -> list[FeedPhase]:
        return [r.phase for r in self.readings]

    def events(self, kind: str) -> list:
        """Every DISTINCT event of one kind the run emitted, in sequence order."""
        seen = {}
        for reading in self.readings:
            event = getattr(reading, kind)
            if event is not None:
                seen[event.seq] = event
        return [seen[seq] for seq in sorted(seen)]

    def assert_never_air(self) -> None:
        """No AIR, and no run of six EMPTY_UNCONFIRMED frames (a firmware change is never air)."""
        assert AIR not in self.phases()
        assert self.events("air_began") == []
        run = 0
        for phase in self.phases():
            run = run + 1 if phase is EMPTY_UNCONFIRMED else 0
            assert run < feed_state._AIR_CONFIRM_FRAMES


def _fed_to(p: _Printer, tray: int, upto: int) -> None:
    for layer in range(1, upto + 1):
        p.push(tray, layer=layer)


# --- the three 2026-10-10 auto-switches ----------------------------------------------------------


class TestTheAutoSwitches:
    def test_005_h2s_the_pre_flip_stays_inside_the_tail_and_the_switch_completes(self):
        """005-H2S: T1 drains 17:05:30, ``tray_now`` pre-flips 1→0 ~1 s later, 255 at 17:10:16
        with the posture not yet 1, the posture, 0x30001, the backup load lands in slot 0 at
        17:10:34. One ``auto_switched(1, 0)``; the tail is charged to tray 1 until the landing."""
        p = _Printer()
        p.start(1)
        _fed_to(p, 1, 100)
        assert p.phase is FED and p.last.feeder == 1

        p.push(1, layer=101, absent={1})  # 17:05:30 — the roll ran dry at the AMS
        assert (p.phase, p.last.tail) == (TAIL, 1)
        p.push(0, layer=101, absent={1})  # ~1 s later — the pre-flip to the backup
        assert (p.phase, p.last.tail, p.last.feeder) == (TAIL, 1, 1)
        for layer in range(101, 112):  # the tail feeds for minutes; layers advance
            p.push(0, layer=layer, absent={1})
        assert (p.phase, p.last.feeder) == (TAIL, 1)

        p.push(255, layer=111, absent={1})  # 17:10:16 — 255, posture not yet 1
        assert (p.phase, p.last.cause, p.last.departed) == (CHANGING, ChangeCause.RUNOUT_SWITCH, 1)
        p.push(255, layer=111, absent={1})
        p.push(255, layer=111, posture=True, absent={1})  # the posture
        assert (p.phase, p.last.cause, p.last.departed) == (CHANGING, ChangeCause.POSTURE, 1)
        for _ in range(3):  # 0x00030001
            p.push(255, layer=111, posture=True, pull_back=True, absent={1})
        p.push(0, layer=111, posture=True, pull_back=True, absent={1})  # 17:10:34 the load lands
        assert p.phase is CHANGING
        p.push(0, layer=111, pull_back=True, auto_switched=True, absent={1})  # 0x30002, posture down
        assert (p.phase, p.last.feeder) == (FED, 0)
        for layer in range(112, 116):
            p.push(0, layer=layer, auto_switched=True, absent={1})

        p.assert_never_air()
        assert CHANGING in p.phases()
        assert [(e.departed, e.arrived) for e in p.events("auto_switched")] == [(1, 0)]
        assert [(e.extruder, e.tray) for e in p.events("roll_drained")] == [(0, 1)]
        assert [e.tray for e in p.events("load_landed")] == [0]
        assert p.last.extruder(0).segments == ((1, 0), (0, 111))
        assert p.last.tray_change_log == ((1, 0), (0, 111))
        assert p.last.last_loaded == 0
        assert p.last.first_unfed_layer is None

    def test_015_h2s_no_pre_flip_and_the_switch_reads_from_255(self):
        """015-H2S: T0 drains 17:23:18, NO pre-flip, 255 at 17:25:52, the load lands in slot 1
        at 17:26:08. The old sampler needed a fed→fed edge and never saw A→255→B."""
        p = _Printer()
        p.start(0)
        _fed_to(p, 0, 4)
        p.push(0, layer=5, absent={0})  # 17:23:18
        assert (p.phase, p.last.tail) == (TAIL, 0)
        for _ in range(8):
            p.push(0, layer=5, absent={0})
        p.push(255, layer=5, absent={0})  # 17:25:52
        p.push(255, layer=5, absent={0})
        p.push(255, layer=5, posture=True, absent={0})
        p.push(255, layer=5, posture=True, pull_back=True, absent={0})
        p.push(1, layer=5, posture=True, pull_back=True, absent={0})  # 17:26:08 the landing
        p.push(1, layer=5, auto_switched=True, absent={0})
        p.push(1, layer=6, auto_switched=True, absent={0})

        p.assert_never_air()
        assert p.phase is FED
        assert [(e.departed, e.arrived) for e in p.events("auto_switched")] == [(0, 1)]
        assert p.last.extruder(0).segments == ((0, 0), (1, 5))

    def test_001_h2s_the_pre_flip_target_is_not_the_landing(self):
        """001-H2S: pre-flip 1→0 17:39:42, 255 17:42:17, the load lands in slot 2 17:42:32 —
        tray 0 never fed, so it never enters the segments."""
        p = _Printer()
        p.start(1)
        _fed_to(p, 1, 124)
        p.push(1, layer=125, absent={1})
        p.push(0, layer=125, absent={1})  # the pre-flip
        for _ in range(5):
            p.push(0, layer=125, absent={1})
        p.push(255, layer=125, absent={1})
        p.push(255, layer=125, posture=True, absent={1})
        p.push(255, layer=125, posture=True, pull_back=True, absent={1})
        p.push(2, layer=125, posture=True, absent={1})  # 17:42:32
        p.push(2, layer=125, auto_switched=True, absent={1})

        p.assert_never_air()
        assert [(e.departed, e.arrived) for e in p.events("auto_switched")] == [(1, 2)]
        assert [e.tray for e in p.events("load_landed")] == [2]
        assert p.last.tray_change_log == ((1, 0), (2, 125))

    def test_the_switch_word_ends_the_tail_when_the_landing_is_the_pre_flip(self):
        """0x00030002 standing with the active feed on another tray: the firmware REPORTED the
        switch, so the tail is over (FED(y)) — and the HMS word stamps no ``auto_switched`` (the
        HMS-edge lane already stamps on that word)."""
        p = _Printer()
        p.start(1)
        _fed_to(p, 1, 10)
        p.push(1, layer=11, absent={1})
        p.push(0, layer=11, absent={1})
        assert p.phase is TAIL
        p.push(0, layer=11, absent={1}, auto_switched=True)

        assert (p.phase, p.last.feeder) == (FED, 0)
        assert p.events("auto_switched") == []
        assert [e.tray for e in p.events("load_landed")] == [0]


# --- runouts the firmware does not rescue --------------------------------------------------------


class TestTheRunoutHolds:
    def test_001_h2s_a_pre_flip_onto_an_empty_slot_fails_into_the_hold(self):
        """001-H2S 18:22–18:51: pre-flip 2→0 onto an EMPTY slot 0 → 255 → the switch fails →
        PAUSE under the slot-agnostic ``0700_8011`` hold → a person loads → RUNNING → the firmware
        refills → FED."""
        p = _Printer()
        p.start(2)
        _fed_to(p, 2, 80)
        p.push(2, layer=81, absent={0, 2})  # T2 drains
        p.push(0, layer=81, absent={0, 2})  # the pre-flip — onto an EMPTY slot
        assert (p.phase, p.last.tail) == (TAIL, 2)
        p.push(255, layer=81, absent={0, 2})
        p.push(255, layer=81, posture=True, absent={0, 2})
        assert p.last.departed == 2
        p.push(255, layer=81, gcode="PAUSE", posture=True, held=True, absent={0, 2})  # 18:25:23
        assert (p.phase, p.last.held_slot) == (RUNOUT_HELD, None)
        for _ in range(4):
            p.push(255, layer=81, gcode="PAUSE", held=True, absent={0, 2})
        p.push(255, layer=81, gcode="PAUSE", held=True, absent={0})  # a person loads slot 2
        assert p.phase is RUNOUT_HELD
        p.push(255, layer=81, held=True, absent={0})  # 18:51 — resumed
        assert (p.phase, p.last.cause) == (CHANGING, ChangeCause.RUNOUT_RESUME)
        p.push(255, layer=81, posture=True, absent={0})
        p.push(2, layer=81, posture=True, absent={0})
        p.push(2, layer=81, absent={0})
        p.push(2, layer=82, absent={0})

        p.assert_never_air()
        assert p.phase is FED
        assert p.events("auto_switched") == []
        assert [e.tray for e in p.events("load_landed")] == [2]
        assert p.last.tray_change_log == ((2, 0),), "neither the pre-flip nor the refill of the same slot appends"

    def test_a_no_backup_runout_holds_for_the_same_slot_and_resumes(self):
        """009-H2S shape: no backup group — the slotted demand holds the print; the refill and
        the resume put the same slot back."""
        p = _Printer()
        p.start(0)
        _fed_to(p, 0, 40)
        p.push(0, layer=41, absent={0})
        p.push(255, layer=41, absent={0})
        p.push(255, layer=41, posture=True, pull_back=True, absent={0})
        p.push(255, layer=41, gcode="PAUSE", demand=(0, 0), pull_back=True, absent={0})
        assert (p.phase, p.last.held_slot) == (RUNOUT_HELD, (0, 0))
        p.push(255, layer=41, gcode="PAUSE", demand=(0, 0))  # refilled; the latch outlives the fix
        p.push(255, layer=41, demand=(0, 0))  # resumed
        assert (p.phase, p.last.cause) == (CHANGING, ChangeCause.RUNOUT_RESUME)
        p.push(255, layer=41, posture=True)
        p.push(0, layer=41)

        p.assert_never_air()
        assert p.phase is FED
        assert p.events("auto_switched") == []


# --- the firmware's change against a print on air ---------------------------------------------------


class TestChangeOrAir:
    def test_shape_41_the_tray_field_leads_the_posture(self):
        """012-H2S: 255 lands up to ~3 s before the posture — EMPTY_UNCONFIRMED, then
        CHANGING(posture) before six quiet votes."""
        p = _Printer()
        p.start(2)
        _fed_to(p, 2, 50)
        for _ in range(3):
            p.push(255, layer=50)
        assert p.phase is EMPTY_UNCONFIRMED
        p.push(255, layer=50, posture=True)
        assert (p.phase, p.last.cause) == (CHANGING, ChangeCause.POSTURE)
        for _ in range(4):
            p.push(255, layer=50, posture=True)
        p.push(2, layer=50)

        p.assert_never_air()
        assert p.phase is FED
        assert p.last.first_unfed_layer is None

    def test_shape_46_a_held_pull_back_drains_under_running_and_the_advance_is_air(self):
        """011/014-H2S: an accepted pull-back ran on its own under a RUNNING print — no posture,
        no runout word — and the first layer advance proves air."""
        p = _Printer()
        p.start(2)
        _fed_to(p, 2, 92)
        p.push(255, layer=93)
        assert p.phase is EMPTY_UNCONFIRMED
        assert p.last.first_unfed_layer is None
        p.push(255, layer=94)

        assert p.phase is AIR
        assert [e.layer for e in p.events("air_began")] == [93]
        assert p.last.first_unfed_layer == 93

    def test_shape_39_a_screen_resume_onto_an_empty_toolhead_confirms_air_in_six_quiet_reports(self):
        p = _Printer()
        p.start(2)
        _fed_to(p, 2, 59)
        p.push(255, layer=60, gcode="PAUSE")
        assert p.phase is EMPTY_PAUSED
        for vote in range(1, feed_state._AIR_CONFIRM_FRAMES):
            p.push(255, layer=60)
            assert p.phase is EMPTY_UNCONFIRMED, vote
        p.push(255, layer=60)

        assert p.phase is AIR
        assert [e.layer for e in p.events("air_began")] == [60]
        assert p.last.first_unfed_layer is None, "no layer was printed yet"

    def test_shape_39_without_the_posture_on_the_wire_nothing_votes_until_a_layer_advances(self):
        """A push that did not carry ``ams_status`` neither votes nor breaks the streak
        (invariant 12's corroboration rule)."""
        p = _Printer()
        p.start(2)
        _fed_to(p, 2, 59)
        p.push(255, layer=60, gcode="PAUSE")
        for _ in range(12):
            p.push(255, layer=60, carried=False)
        assert p.phase is EMPTY_UNCONFIRMED
        p.push(255, layer=61, carried=False)

        assert p.phase is AIR
        assert p.last.first_unfed_layer == 60

    def test_a_silent_report_neither_votes_nor_breaks_the_streak(self):
        p = _Printer()
        p.start(2)
        _fed_to(p, 2, 20)
        for _ in range(3):
            p.push(255, layer=20)
        p.push(255, layer=20, carried=False)
        for _ in range(2):
            p.push(255, layer=20)
        assert p.phase is EMPTY_UNCONFIRMED
        p.push(255, layer=20)
        assert p.phase is AIR

    def test_a_runout_word_resets_the_streak(self):
        p = _Printer()
        p.start(2)
        _fed_to(p, 2, 20)
        for _ in range(5):
            p.push(255, layer=20)
        p.push(255, layer=20, pull_back=True)
        for _ in range(5):
            p.push(255, layer=20)
        assert p.phase is EMPTY_UNCONFIRMED
        p.push(255, layer=20)
        assert p.phase is AIR

    def test_a_false_tail_flaps_back_to_fed(self):
        """A presence flap: the feeding tray reads absent, then present again — no switch."""
        p = _Printer()
        p.start(1)
        _fed_to(p, 1, 30)
        p.push(1, layer=31, absent={1})
        assert p.phase is TAIL
        p.push(1, layer=31)

        assert (p.phase, p.last.feeder) == (FED, 1)
        assert p.events("auto_switched") == []
        assert p.last.tray_change_log == ((1, 0),)

    def test_a_false_tail_whose_change_never_comes_exits_to_air_on_a_layer_advance(self):
        """An operator pull mid-print: TAIL, then 255 with no firmware sequence. Every CHANGING
        cause keeps the two exits to AIR, so a false TAIL never hides air to the FINISH."""
        p = _Printer()
        p.start(1)
        _fed_to(p, 1, 30)
        p.push(1, layer=31, absent={1})
        p.push(255, layer=31, absent={1})
        assert (p.phase, p.last.cause) == (CHANGING, ChangeCause.RUNOUT_SWITCH)
        p.push(255, layer=31, absent={1})
        p.push(255, layer=32, absent={1})

        assert p.phase is AIR
        assert p.last.first_unfed_layer == 31

    def test_a_false_tail_whose_change_never_comes_exits_to_air_on_six_quiet_votes(self):
        p = _Printer()
        p.start(1)
        _fed_to(p, 1, 30)
        p.push(1, layer=31, absent={1})
        for _ in range(feed_state._AIR_CONFIRM_FRAMES):
            p.push(255, layer=31, absent={1})

        assert p.phase is AIR


# --- the start block, the end retract ----------------------------------------------------------------


class TestTheJobsEnds:
    def test_the_start_block_reads_empty_as_the_load_still_to_come(self):
        p = _Printer()
        p.start(255)
        assert p.phase is START_BLOCK
        p.push(255, layer=0)
        p.push(2, layer=0)
        assert p.phase is START_BLOCK
        p.push(2, layer=1)
        assert p.phase is FED
        assert p.last.tray_change_log == ((2, 0),)

    def test_a_pause_at_layer_0_is_the_start_block(self):
        """012-H2S paused at layer 0 at the plate-marker dialog."""
        p = _Printer()
        p.start(255)
        p.push(255, layer=0, gcode="PAUSE")
        assert p.phase is START_BLOCK

    def test_the_end_retract_at_the_last_layer_while_running(self):
        p = _Printer()
        p.start(2)
        p.push(2, layer=_TOTAL - 1)
        p.push(2, layer=_TOTAL)
        p.push(255, layer=_TOTAL)
        assert p.phase is END_RETRACT
        assert p.last.last_loaded == 2, "the end retract's 255 never moves the last feeder"
        assert p.last.first_unfed_layer is None

    def test_a_pause_at_the_last_layer_is_not_the_end_retract(self):
        p = _Printer()
        p.start(2)
        p.push(2, layer=_TOTAL - 1)
        p.push(255, layer=_TOTAL, gcode="PAUSE")
        assert p.phase is EMPTY_PAUSED

    def test_an_unknown_total_stamps_no_unfed_layer(self):
        p = _Printer()
        p.start(2, total=0)
        p.push(2, layer=40, total=0)
        p.push(255, layer=40, total=0)
        p.push(255, layer=41, total=0)
        assert p.phase is AIR
        assert p.last.first_unfed_layer is None

    def test_the_job_gate_reads_idle_and_a_setup_state_keeps_the_phase(self):
        """P2S #957 leaves RUNNING during the auto-fallback: the lifecycle gate, never RUNNING,
        decides whether a job runs."""
        p = _Printer()
        p.start(1)
        _fed_to(p, 1, 10)
        p.push(1, layer=10, gcode="LOADING")
        assert p.phase is FED
        p.push(255, layer=10, gcode="FINISH", job=False)
        assert p.phase is IDLE


# --- sessions and attaches ------------------------------------------------------------------------------


class TestSessions:
    def test_a_reconnect_reads_unknown_and_re_seeds_without_events(self):
        p = _Printer()
        p.start(1)
        _fed_to(p, 1, 10)
        landed = len(p.events("load_landed"))
        p.push(3, layer=10, live=False, epoch=2)  # the new session's cached broadcast
        assert p.phase is UNKNOWN
        p.push(3, layer=10, epoch=2)  # its first fresh report — a reading, not an event
        assert (p.phase, p.last.feeder) == (FED, 3)
        assert len(p.events("load_landed")) == landed
        p.push(2, layer=10, epoch=2)
        assert [e.tray for e in p.events("load_landed")][-1] == 2

    def test_an_attach_mid_tail_seeds_the_tail_without_an_event(self):
        """Bambuddy restarted while a roll's tail fed: the attach reading is the phase, never a
        drain the farm witnessed — and the switch after it IS witnessed."""
        p = _Printer()
        p.push(0, layer=70, live=False, boundary=JobBoundary.ATTACH, absent={0})
        p.push(0, layer=70, absent={0})
        assert (p.phase, p.last.tail) == (TAIL, 0)
        assert p.events("roll_drained") == []
        assert p.last.tray_change_log == ()
        p.push(255, layer=70, absent={0})
        p.push(255, layer=70, posture=True, absent={0})
        p.push(1, layer=70, absent={0})

        assert [(e.departed, e.arrived) for e in p.events("auto_switched")] == [(0, 1)]
        assert p.last.tray_change_log == ((1, 70),)

    def test_an_attach_keeps_the_last_feeder_and_clears_the_jobs_record(self):
        p = _Printer()
        p.start(1)
        _fed_to(p, 1, 10)
        p.push(255, layer=11)
        p.push(255, layer=12)
        assert p.last.first_unfed_layer == 11
        p.push(255, layer=5, gcode="PAUSE", boundary=JobBoundary.ATTACH)

        assert p.last.tray_change_log == ()
        assert p.last.first_unfed_layer is None
        assert p.last.last_loaded == 1
        assert p.last.extruder(0).last_fed == 1

    def test_a_new_job_resets_the_record_and_seeds_its_start_feeder(self):
        p = _Printer()
        p.start(1)
        _fed_to(p, 1, 10)
        p.push(255, layer=11)
        p.push(255, layer=12)
        p.push(255, layer=12, gcode="FINISH", job=False)
        p.push(3, layer=0, boundary=JobBoundary.NEW_JOB)

        assert p.last.tray_change_log == ((3, 0),)
        assert p.last.first_unfed_layer is None
        assert p.last.last_loaded == 3


# --- the dual-nozzle H2C (synthetic: a real capture is owed) -------------------------------------------


class TestDualNozzle:
    def test_a_tool_change_is_the_firmwares_change_and_its_load_lands(self):
        p = _Printer()
        p.start(1, feed=_dual(0, {0: 1, 1: 255}))
        for layer in range(1, 6):
            p.push(layer=layer, feed=_dual(0, {0: 1, 1: 255}))
        assert (p.phase, p.last.feeder) == (FED, 1)
        p.push(layer=6, feed=_dual(1, {0: 1, 1: 255}))
        assert (p.phase, p.last.cause) == (CHANGING, ChangeCause.TOOL_CHANGE)
        p.push(layer=6, feed=_dual(1, {0: 1, 1: 5}))

        assert (p.phase, p.last.active_extruder, p.last.feeder) == (FED, 1, 5)
        assert [(e.extruder, e.tray) for e in p.events("load_landed")] == [(1, 5)]
        assert p.last.extruder(0).segments == ((1, 0),)
        assert p.last.extruder(1).segments == ((5, 6),)
        assert p.last.tray_change_log == ((1, 0), (5, 6))
        p.assert_never_air()

    def test_the_inactive_extruders_roll_draining_is_its_own_event(self):
        p = _Printer()
        p.start(1, feed=_dual(0, {0: 1, 1: 5}))
        for layer in range(1, 6):
            p.push(layer=layer, feed=_dual(0, {0: 1, 1: 5}))
        p.push(layer=6, feed=_dual(0, {0: 1, 1: 5}), absent={5})
        p.push(layer=7, feed=_dual(0, {0: 1, 1: 5}), absent={5})

        assert (p.phase, p.last.feeder) == (FED, 1)
        assert [(e.extruder, e.tray) for e in p.events("roll_drained")] == [(1, 5)]
        assert p.last.extruder(1).drained == frozenset({5})
        assert p.last.extruder(0).drained == frozenset()


# --- an idle printer -----------------------------------------------------------------------------------


class TestIdleLoads:
    def test_an_operators_load_on_an_idle_printer_lands(self):
        """Repair evidence: a person's Load after clearing a physical fault (MotionLedger parity —
        any gcode state)."""
        p = _Printer()
        for _ in range(2):
            p.push(255, gcode="IDLE", job=False, layer=0)
        assert p.phase is IDLE
        p.push(2, gcode="IDLE", job=False, layer=0)

        (landed,) = p.events("load_landed")
        assert (landed.tray, landed.from_phase, landed.farm_acting) == (2, IDLE, False)
        assert p.last.tray_change_log == (), "no job, no segment"
        assert p.last.last_loaded == 2

    def test_a_load_under_a_farm_motion_carries_farm_acting(self):
        p = _Printer()
        for _ in range(2):
            p.push(255, gcode="IDLE", job=False, layer=0)
        p.push(3, gcode="IDLE", job=False, layer=0, farm_acting=True)

        (landed,) = p.events("load_landed")
        assert landed.farm_acting is True

    def test_the_external_holder_is_a_feeder_but_no_landing(self):
        p = _Printer()
        for _ in range(2):
            p.push(255, gcode="IDLE", job=False, layer=0)
        p.push(254, gcode="IDLE", job=False, layer=0)

        assert p.events("load_landed") == []
        assert (p.last.feeder, p.last.last_loaded) == (254, 254)


# --- the sequence ------------------------------------------------------------------------------------------


class TestTheSequence:
    def test_events_are_ordered_by_one_process_global_sequence(self):
        """A reconnect builds a new client: a per-instance counter would restart under a
        consumer's cursor."""
        first = _Printer()
        first.start(1)
        _fed_to(first, 1, 5)
        first.push(1, layer=6, absent={1})
        drained = first.events("roll_drained")[0]

        second = _Printer()
        for _ in range(2):
            second.push(255, gcode="IDLE", job=False, layer=0)
        second.push(2, gcode="IDLE", job=False, layer=0)
        landed = second.events("load_landed")[0]

        assert landed.seq > drained.seq

    def test_within_one_frame_the_landing_precedes_the_switch(self):
        p = _Printer()
        p.start(1)
        _fed_to(p, 1, 5)
        p.push(1, layer=6, absent={1})
        p.push(255, layer=6, absent={1})
        p.push(255, layer=6, posture=True, absent={1})
        p.push(0, layer=6, absent={1})  # lands and leaves the change in one push

        (landed,) = p.events("load_landed")
        (switched,) = p.events("auto_switched")
        assert isinstance(switched, AutoSwitched)
        assert landed.seq < switched.seq


# --- the table ---------------------------------------------------------------------------------------------


class TestTheTable:
    def test_the_table_is_total_over_phase_and_signal(self):
        """The lookup RAISES on a missing cell; completeness here is what keeps that raise off
        the MQTT thread."""
        missing = [
            (phase, signal)
            for phase in FeedPhase
            for signal in FeedSignal
            if (phase, signal) not in feed_state._TRANSITIONS
        ]
        assert missing == []
        assert len(feed_state._TRANSITIONS) == len(FeedPhase) * len(FeedSignal)

    def test_a_missing_cell_raises(self):
        with pytest.raises(LookupError):
            feed_state._transition(FeedPhase.FED, "not-a-signal")  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("broken", "phase"),
        [
            # Malformed past reading: the step fails closed to UNKNOWN and re-seeds.
            pytest.param({"feed": None}, UNKNOWN, id="no_feed"),
            pytest.param({"presence": None}, UNKNOWN, id="no_presence"),
            pytest.param({"layer": "x"}, UNKNOWN, id="a_text_layer"),
            pytest.param({"total_layers": None}, UNKNOWN, id="no_total"),
            pytest.param({"epoch": None}, UNKNOWN, id="no_epoch"),
            # Readable, just unplaceable: a gcode state the job gate cannot place keeps the phase.
            pytest.param({"gcode_state": None}, FED, id="no_state"),
        ],
    )
    def test_step_never_raises_on_a_malformed_frame(self, broken, phase):
        p = _Printer()
        p.start(1)
        p.push(1, layer=3)
        good = frame(1, layer=4)
        reading = p.tracker.step(FeedFrame(**{**good.__dict__, **broken}))

        assert reading.phase is phase
        # The frames after it still step (an unreadable epoch is a new session: it takes the one
        # re-seeding frame more).
        p.tracker.step(frame(1, layer=5))
        assert p.tracker.step(frame(1, layer=6)).phase is FED

    def test_the_initial_reading_is_unknown(self):
        reading = FeedTracker().reading
        assert reading.phase is UNKNOWN
        assert (reading.feeder, reading.last_loaded, reading.first_unfed_layer) == (None, -1, None)
        assert reading.tray_change_log == ()


# --- the frame, read off a real printer state ----------------------------------------------------------


def _hms(attr: int, code: int) -> HMSError:
    return HMSError(code=hex(code), attr=attr, module=(attr >> 24) & 0xFF, severity=3)


class TestTheFrame:
    def test_of_reads_the_wire_through_its_owners(self):
        state = PrinterState()
        state.connection_epoch = 4
        state.state = "RUNNING"
        state.tray_now = 1
        state.total_layers = 120
        state.raw_data = {"ams": [{"id": 0, "tray": [{"id": 1, "state": 9, "tray_type": ""}]}]}
        state.hms_errors = [_hms(0x07002100, 0x00030001), _hms(0x07002100, 0x00030002), _hms(0x07000000, 0x8011)]

        f = FeedFrame.of(
            state,
            live=True,
            posture_carried=True,
            posture=False,
            layer=33,
            job_active=True,
            farm_acting=False,
            boundary=JobBoundary.NONE,
        )

        assert (f.epoch, f.gcode_state, f.layer, f.total_layers) == (4, "RUNNING", 33, 120)
        assert f.feed.active == tray_fields.ExtruderFeed("fed", 1)
        assert f.reads_absent(1) is True
        assert (f.pull_back, f.auto_switched, f.runout_demand_standing, f.runout_demand) == (True, True, True, None)

    def test_the_slotted_demand_is_the_held_slot(self):
        state = PrinterState()
        state.hms_errors = [_hms(0x07002200, 0x00020001)]
        f = FeedFrame.of(
            state,
            live=True,
            posture_carried=False,
            posture=False,
            layer=1,
            job_active=True,
            farm_acting=False,
            boundary=JobBoundary.NONE,
        )
        assert (f.runout_demand, f.runout_demand_standing, f.pull_back, f.auto_switched) == ((0, 2), True, False, False)


class TestTheDerivedRunoutViews:
    def test_the_two_spent_words_split_by_what_they_report(self):
        """Derived in ``hms_errors``, never re-spelled: the auto-switch report is the spent word
        whose alert is suppressed because it reports finished work; the pull-back is the other."""
        assert frozenset({0x00030002}) == hms_errors._RUNOUT_AUTO_SWITCHED_CODE32
        assert frozenset({0x00030001}) == hms_errors._RUNOUT_PULL_BACK_CODE32

    def test_the_words_are_read_under_a_tray_attr_only(self):
        """The short form ``0700_0002`` is also the assist-motor overload's; only the slotted
        word under a TRAY attr is the auto-switch report."""
        assert hms_errors.auto_switch_standing([_hms(0x07002200, 0x00030002)]) is True
        assert hms_errors.auto_switch_standing([_hms(0x07000100, 0x00020002)]) is False
        assert hms_errors.pull_back_standing([_hms(0x07002000, 0x00030001)]) is True
        assert hms_errors.pull_back_standing([_hms(0x07002000, 0x00030002)]) is False

    def test_the_demand_standing_covers_the_slot_agnostic_hold(self):
        assert hms_errors.runout_demand_standing([_hms(0x07000000, 0x8011)]) is True
        assert hms_errors.runout_demand_standing([_hms(0x07002300, 0x00020001)]) is True
        assert hms_errors.runout_demand_standing([_hms(0x07002300, 0x00030002)]) is False
        assert hms_errors.runout_demand_standing([object()]) is False
