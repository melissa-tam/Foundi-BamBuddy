"""``services/eject/bed_motion`` — the one builder of the farm's bed-motion G-code and its claimed send.

The G-code of both motions must stay BYTE-IDENTICAL to the inline builders it replaced in
``farm_policy`` (the goldens below are those builders' own f-strings), the held-bed lift must never
carry an absolute Z (the one motion allowed over an occupied plate — 002-H2S bed-into-the-floor), and
the lift's send is CLAIMED on the plate authority: refused under a running job, a dispatch or an
eject in flight, or another motion. (The idle deep-park is sent unclaimed by ``farm_policy``, as before.)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.eject import bed_motion
from backend.app.services.plate_occupancy import EscalationOnly, Evidence, PendingEject, plate_occupancy


def _pre_refactor_lift(lift: float) -> str:
    """``farm_policy._maybe_lift_held_bed``'s inline builder before the extraction (2026-10-12 build)."""
    feed = 1200
    return f"M17\nG91\nG380 S2 Z{32.0:.1f} F{feed}\nG380 S2 Z-{lift:.1f} F{feed}\nG90\nM400\nM18"


def _pre_refactor_park(park_z: float) -> str:
    """``farm_policy._maybe_idle_deep_park``'s inline builder before the extraction."""
    return f"M17\nG90\nG1 Z{park_z:.1f} F900\nM400\nM18"


class TestTheGcodeIsByteIdentical:
    @pytest.mark.parametrize("lift", [12.0, 15.0, 33.333, 60.0])
    def test_the_held_bed_lift(self, lift):
        assert bed_motion.held_bed_lift_gcode(lift) == _pre_refactor_lift(lift)

    def test_the_held_bed_lift_golden(self):
        assert (
            bed_motion.held_bed_lift_gcode(12.0)
            == "M17\nG91\nG380 S2 Z32.0 F1200\nG380 S2 Z-12.0 F1200\nG90\nM400\nM18"
        )

    @pytest.mark.parametrize("park_z", [255.0, 34.0, 323.0, 243.75])
    def test_the_idle_deep_park(self, park_z):
        assert bed_motion.idle_deep_park_gcode(park_z) == _pre_refactor_park(park_z)

    def test_the_lift_seconds_are_both_guarded_moves_in_full(self):
        assert bed_motion.held_bed_lift_seconds(15.0) == pytest.approx((32.0 + 15.0) / (1200 / 60))


class TestTheLiftNeverMovesToAnAbsoluteZ:
    def test_every_move_is_a_guarded_relative_drive(self):
        lines = bed_motion.held_bed_lift_gcode(12.0).splitlines()
        relative = False
        for line in lines:
            word = line.split()[0]
            if word == "G91":
                relative = True
            elif word == "G90":
                relative = False
            assert word not in ("G0", "G1"), line
            if word == "G380":
                assert relative, f"{line} outside relative mode"
        # Absolute mode is restored before the steppers are released.
        assert lines[-3:] == ["G90", "M400", "M18"]


class _Client:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[str] = []

    def send_gcode(self, gcode: str, *, sequence_id=None) -> bool:
        self.sent.append(gcode)
        return self.ok


async def _printer(db, model="H2S") -> Printer:
    printer = Printer(
        name=f"BM-{model}", serial_number=f"SBM{model}", ip_address="1.2.3.4", access_code="x", model=model
    )
    db.add(printer)
    await db.commit()
    await db.refresh(printer)
    return printer


class TestTheClaimedLift:
    async def test_a_granted_lift_is_sent_through_the_record_and_holds_dispatch(self, db_session, seed_geometry):
        printer = await _printer(db_session)
        client = _Client()
        with patch.object(bed_motion.printer_manager, "get_client", return_value=client):
            outcome = await bed_motion.lift_held_bed(db_session, printer.id)

        assert isinstance(outcome, bed_motion.MotionSent)
        assert outcome.height_mm == 12.0
        assert client.sent == [bed_motion.held_bed_lift_gcode(12.0)]
        assert plate_occupancy.dispatchable(printer.id, Evidence(live_state="IDLE")) == "bed_motion_in_flight"

    @pytest.mark.parametrize("refusal", ["job_active", "dispatch_in_flight", "eject_in_flight", "bed_motion_in_flight"])
    async def test_a_refused_claim_sends_nothing(self, db_session, seed_geometry, refusal):
        printer = await _printer(db_session)
        live = "RUNNING" if refusal == "job_active" else "IDLE"
        if refusal == "dispatch_in_flight":
            db_session.add(
                PrintQueueItem(printer_id=printer.id, status="printing", plate_id=1, position=1, first_article=False)
            )
            await db_session.commit()
        if refusal == "eject_in_flight":
            plate_occupancy.hydrate_plate(printer.id, "SUB-1", EscalationOnly())
            assert plate_occupancy.claim_for_eject(printer.id, PendingEject("manual", None, None), Evidence()) is None
        if refusal == "bed_motion_in_flight":
            assert plate_occupancy.claim_bed_motion(printer.id, Evidence(live_state="IDLE"), hold_s=30.0) is None
        client = _Client()
        with (
            patch.object(bed_motion.printer_manager, "get_client", return_value=client),
            patch.object(bed_motion.printer_manager, "get_status", return_value=SimpleNamespace(state=live)),
        ):
            outcome = await bed_motion.lift_held_bed(db_session, printer.id)

        assert outcome == bed_motion.MotionSkipped(refusal)
        assert not outcome.permanent
        assert client.sent == []

    async def test_a_send_the_printer_did_not_take_releases_the_claim(self, db_session, seed_geometry):
        printer = await _printer(db_session)
        client = _Client(ok=False)
        with patch.object(bed_motion.printer_manager, "get_client", return_value=client):
            outcome = await bed_motion.lift_held_bed(db_session, printer.id)

        assert outcome == bed_motion.MotionSkipped("not_sent")
        assert len(client.sent) == 1
        assert plate_occupancy.snapshot(printer.id).bed_motion_active is False

    async def test_no_client_is_a_transient_skip(self, db_session, seed_geometry):
        printer = await _printer(db_session)
        with patch.object(bed_motion.printer_manager, "get_client", return_value=None):
            outcome = await bed_motion.lift_held_bed(db_session, printer.id)
        assert outcome == bed_motion.MotionSkipped("no_client")
        assert not outcome.permanent

    async def test_a_bedslinger_or_a_model_with_no_z_travel_is_a_permanent_skip(self, db_session, seed_geometry):
        bedslinger = await _printer(db_session, model="A1")
        no_geometry = await _printer(db_session, model="P1S")
        client = _Client()
        with patch.object(bed_motion.printer_manager, "get_client", return_value=client):
            first = await bed_motion.lift_held_bed(db_session, bedslinger.id)
            second = await bed_motion.lift_held_bed(db_session, no_geometry.id)

        assert first == bed_motion.MotionSkipped("bedslinger")
        assert second == bed_motion.MotionSkipped("no_z_travel")
        assert first.permanent and second.permanent
        assert client.sent == []
