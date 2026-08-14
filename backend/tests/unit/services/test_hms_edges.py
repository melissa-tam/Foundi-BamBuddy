"""Tests for hms_edges — per-printer wire-HMS appearance-edge detection.

Real PrinterState + real HMSError objects (the type bambu_mqtt's hms[] branch
appends), so the entry-field access the module does is pinned against the shape
production actually produces.
"""

import logging

import pytest

from backend.app.services import hms_edges
from backend.app.services.bambu_mqtt import HMSError, PrinterState
from backend.app.services.hms_errors import hms_severity

# Two real fleet codes: 07FF_8011 (filament runout) and 0300_4057 (Z step loss).
RUNOUT_ATTR, RUNOUT_CODE = 0x07FF0200, 0x8011
RUNOUT_FULL, RUNOUT_SHORT = "07FF020000008011", "07FF_8011"
STEPLOSS_ATTR, STEPLOSS_CODE = 0x03000100, 0x4057
STEPLOSS_FULL, STEPLOSS_SHORT = "0300010000004057", "0300_4057"


def _hms(attr: int, code: int) -> HMSError:
    """Build an HMSError exactly as bambu_mqtt's hms[] branch does."""
    return HMSError(
        code=f"0x{code:x}",
        attr=attr,
        module=(attr >> 24) & 0xFF,
        severity=hms_severity(code),
        full_code=f"{attr:08X}{code:08X}",
    )


def _state(*, wire_at: float, gcode_state: str = "RUNNING", errors: tuple = ()) -> PrinterState:
    return PrinterState(state=gcode_state, hms_wire_at=wire_at, hms_errors=list(errors))


@pytest.fixture(autouse=True)
def _clean_edge_state():
    hms_edges._reset_state()
    yield
    hms_edges._reset_state()


class TestConsumeGuards:
    """Which frames are worth consuming at all."""

    def test_never_stamped_wire_returns_none_and_does_not_seed(self):
        """hms_wire_at 0.0 means no push has ever carried HMS evidence — an empty
        hms_errors there is "unobserved", not "no faults", so it must not seed."""
        assert hms_edges.note_push(1, _state(wire_at=0.0)) is None
        assert 1 not in hms_edges._edge_state

    def test_unknown_gcode_state_neither_seeds_nor_consumes(self):
        """The connect-time broadcast fires with state="unknown" before any real
        report. Seeding there would record a set from a non-report; consuming the
        timestamp would make the first REAL frame look already-seen."""
        assert hms_edges.note_push(1, _state(wire_at=100.0, gcode_state="unknown")) is None
        assert 1 not in hms_edges._edge_state

        # The same wire_at on a known-state frame is still the seed, not an edge.
        report = hms_edges.note_push(1, _state(wire_at=100.0, errors=(_hms(RUNOUT_ATTR, RUNOUT_CODE),)))
        assert report is None
        assert hms_edges._edge_state[1] == (100.0, frozenset({RUNOUT_FULL}))

    def test_stale_wire_at_returns_none_without_touching_state(self):
        """A push that repeats an unchanged hms_errors list carries no new
        evidence — neither an older nor an equal stamp may be re-consumed."""
        hms_edges.note_push(1, _state(wire_at=100.0))
        hms_edges.note_push(1, _state(wire_at=200.0, errors=(_hms(RUNOUT_ATTR, RUNOUT_CODE),)))
        before = hms_edges._edge_state[1]

        assert hms_edges.note_push(1, _state(wire_at=200.0, errors=(_hms(STEPLOSS_ATTR, STEPLOSS_CODE),))) is None
        assert hms_edges.note_push(1, _state(wire_at=150.0, errors=(_hms(STEPLOSS_ATTR, STEPLOSS_CODE),))) is None
        assert hms_edges._edge_state[1] == before


class TestSeeding:
    """Restart-replay suppression: a code live at first sight never fires."""

    def test_seed_logs_standing_codes(self, caplog):
        """The suppression is deliberate, but it is also what silences every edge-driven
        state consumer for a fault standing across a deploy — a terminal runout can hold
        for hours, so a deploy inside the hold re-seeds its codes and nothing can ever
        edge them again. When a stamp then never lands, this line is what separates "the
        lane is dead" from "the lane was correctly suppressed". One line per printer per
        process: the seed branch runs exactly once."""
        with caplog.at_level(logging.INFO, logger="backend.app.services.hms_edges"):
            hms_edges.note_push(1, _state(wire_at=100.0, errors=(_hms(RUNOUT_ATTR, RUNOUT_CODE),)))
            hms_edges.note_push(1, _state(wire_at=200.0, errors=(_hms(RUNOUT_ATTR, RUNOUT_CODE),)))

        seeds = [r.getMessage() for r in caplog.records if "seeded printer" in r.getMessage()]
        assert len(seeds) == 1
        assert "1 standing HMS code(s)" in seeds[0]
        assert RUNOUT_FULL in seeds[0]

    def test_seed_logs_a_clean_printer_too(self, caplog):
        """A quiet printer seeds as well, and says so — the line marks the boundary the
        edge lane starts from, which is exactly what a triage needs when the question is
        "did this process ever consume a frame for that printer?"."""
        with caplog.at_level(logging.INFO, logger="backend.app.services.hms_edges"):
            hms_edges.note_push(1, _state(wire_at=100.0))

        assert any("0 standing HMS code(s)" in r.getMessage() for r in caplog.records)

    def test_first_known_frame_seeds_without_edging(self):
        report = hms_edges.note_push(1, _state(wire_at=100.0, errors=(_hms(RUNOUT_ATTR, RUNOUT_CODE),)))
        assert report is None
        assert hms_edges._edge_state[1] == (100.0, frozenset({RUNOUT_FULL}))

    def test_seeded_code_standing_across_later_frames_never_edges(self):
        hms_edges.note_push(1, _state(wire_at=100.0, errors=(_hms(RUNOUT_ATTR, RUNOUT_CODE),)))
        assert hms_edges.note_push(1, _state(wire_at=200.0, errors=(_hms(RUNOUT_ATTR, RUNOUT_CODE),))) is None
        assert hms_edges.note_push(1, _state(wire_at=300.0, errors=(_hms(RUNOUT_ATTR, RUNOUT_CODE),))) is None


class TestAppearanceEdges:
    def test_new_code_on_a_later_frame_reports_all_three_shapes(self):
        hms_edges.note_push(1, _state(wire_at=100.0))
        entry = _hms(RUNOUT_ATTR, RUNOUT_CODE)
        report = hms_edges.note_push(1, _state(wire_at=200.0, errors=(entry,)))

        assert report is not None
        assert report.appeared == (entry,)
        assert report.appeared_full == frozenset({RUNOUT_FULL})
        assert report.appeared_short == frozenset({RUNOUT_SHORT})

    def test_only_the_new_code_appears_beside_a_standing_one(self):
        standing = _hms(RUNOUT_ATTR, RUNOUT_CODE)
        hms_edges.note_push(1, _state(wire_at=100.0, errors=(standing,)))
        fresh = _hms(STEPLOSS_ATTR, STEPLOSS_CODE)
        report = hms_edges.note_push(1, _state(wire_at=200.0, errors=(standing, fresh)))

        assert report is not None
        assert report.appeared == (fresh,)
        assert report.appeared_full == frozenset({STEPLOSS_FULL})
        assert report.appeared_short == frozenset({STEPLOSS_SHORT})

    def test_multi_code_appearance_in_one_frame_carries_every_entry(self):
        hms_edges.note_push(1, _state(wire_at=100.0))
        runout = _hms(RUNOUT_ATTR, RUNOUT_CODE)
        steploss = _hms(STEPLOSS_ATTR, STEPLOSS_CODE)
        report = hms_edges.note_push(1, _state(wire_at=200.0, errors=(runout, steploss)))

        assert report is not None
        assert report.appeared == (runout, steploss)
        assert report.appeared_full == frozenset({RUNOUT_FULL, STEPLOSS_FULL})
        assert report.appeared_short == frozenset({RUNOUT_SHORT, STEPLOSS_SHORT})

    def test_code_clearing_on_the_wire_then_returning_edges_again(self):
        """A wire all-clear is real evidence (bambu_mqtt stamps an empty hms list),
        so the code leaves the live set and its RETURN is a genuine appearance —
        this is a fault flapping, not the standing-code case."""
        hms_edges.note_push(1, _state(wire_at=100.0, errors=(_hms(RUNOUT_ATTR, RUNOUT_CODE),)))
        assert hms_edges.note_push(1, _state(wire_at=200.0)) is None  # wire all-clear

        report = hms_edges.note_push(1, _state(wire_at=300.0, errors=(_hms(RUNOUT_ATTR, RUNOUT_CODE),)))
        assert report is not None
        assert report.appeared_full == frozenset({RUNOUT_FULL})


class TestPerPrinterIsolation:
    def test_two_printers_keep_separate_ledgers(self):
        hms_edges.note_push(1, _state(wire_at=100.0, errors=(_hms(RUNOUT_ATTR, RUNOUT_CODE),)))
        hms_edges.note_push(2, _state(wire_at=100.0))

        # Printer 1's code is seeded (no edge); printer 2 has never seen it.
        assert hms_edges.note_push(1, _state(wire_at=200.0, errors=(_hms(RUNOUT_ATTR, RUNOUT_CODE),))) is None
        report = hms_edges.note_push(2, _state(wire_at=200.0, errors=(_hms(RUNOUT_ATTR, RUNOUT_CODE),)))
        assert report is not None
        assert report.appeared_full == frozenset({RUNOUT_FULL})


class TestResetState:
    def test_reset_state_clears_every_printer(self):
        hms_edges.note_push(1, _state(wire_at=100.0, errors=(_hms(RUNOUT_ATTR, RUNOUT_CODE),)))
        hms_edges.note_push(2, _state(wire_at=100.0))
        assert hms_edges._edge_state

        hms_edges._reset_state()
        assert hms_edges._edge_state == {}

        # After the reset the next frame seeds again rather than edging.
        assert hms_edges.note_push(1, _state(wire_at=200.0, errors=(_hms(RUNOUT_ATTR, RUNOUT_CODE),))) is None
