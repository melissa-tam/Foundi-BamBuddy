"""Schema tests for the slot-recency residue on SpoolResponse (2026-09-10).

``spool.last_location_{printer_id,ams_id,tray_id,at}`` is a documented denormalisation
whose ONE writer is ``spool_binding._stamp_last_location`` — it records the AMS slot a
roll was last released or moved from. The Assign-spool picker needs it client-side to
rank "last in this slot" above the rest, which is a DISPLAY projection and never an
identity decision.

Two halves, pinned together because the pair is the contract: it must serialise on the
READ side, and it must stay off the WRITE side. The residue is something the wire
observed; a PATCH stating it would be a second writer for a fact that has one, and the
picker would then be ranking on a claim rather than an observation.
"""

from datetime import datetime, timezone

from backend.app.models.spool import Spool
from backend.app.schemas.spool import SpoolResponse, SpoolUpdate

_AT = datetime(2026, 9, 9, 14, 30, 0, tzinfo=timezone.utc)

# A transient ORM instance has no column defaults applied (those land at flush), so the
# non-nullable weight columns are spelled out here — the residue is what these tests are
# about, and a validation error on an unrelated column would say nothing about it.
_WEIGHTS = {
    "label_weight": 1000,
    "core_weight": 250,
    "weight_used": 0.0,
    "weight_used_baseline": 0.0,
    "weight_locked": False,
}


class TestResidueOnTheResponse:
    def test_response_carries_the_four_residue_fields(self):
        """``from_attributes`` off a real ORM row — the shape the route actually
        serialises, not a hand-built dict that could agree with a wrong schema."""
        now = datetime.now(timezone.utc)
        spool = Spool(
            id=7,
            material="PETG",
            **_WEIGHTS,
            last_location_printer_id=3,
            last_location_ams_id=0,
            last_location_tray_id=2,
            last_location_at=_AT,
            created_at=now,
            updated_at=now,
        )
        response = SpoolResponse.model_validate(spool, from_attributes=True)
        assert response.last_location_printer_id == 3
        assert response.last_location_ams_id == 0, "slot 0 is a real slot — the field must not be falsy-dropped"
        assert response.last_location_tray_id == 2
        assert response.last_location_at == _AT

    def test_residue_defaults_to_null_on_a_roll_that_never_left_a_slot(self):
        now = datetime.now(timezone.utc)
        spool = Spool(id=8, material="PLA", created_at=now, updated_at=now, **_WEIGHTS)
        response = SpoolResponse.model_validate(spool, from_attributes=True)
        assert response.last_location_printer_id is None
        assert response.last_location_ams_id is None
        assert response.last_location_tray_id is None
        assert response.last_location_at is None


class TestResidueIsNotWritable:
    def test_update_ignores_a_residue_field(self):
        """The route applies ``model_dump(exclude_unset=True)`` with a setattr loop, so
        "absent from the schema" IS "cannot be written". Pydantic drops unknown keys by
        default; this pins that SpoolUpdate has not gained them and has not turned on
        ``extra="allow"`` either."""
        update = SpoolUpdate(**{"last_location_at": _AT, "last_location_printer_id": 3, "brand": "Bambu Lab"})
        dumped = update.model_dump(exclude_unset=True)
        assert dumped == {"brand": "Bambu Lab"}

    def test_residue_fields_are_absent_from_the_update_model(self):
        for field in (
            "last_location_printer_id",
            "last_location_ams_id",
            "last_location_tray_id",
            "last_location_at",
        ):
            assert field not in SpoolUpdate.model_fields, f"{field} is server-owned; a PATCH must not state it"
