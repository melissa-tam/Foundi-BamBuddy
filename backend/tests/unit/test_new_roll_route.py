"""Tests for the merged slot verb ``POST /inventory/spools/{id}/new-roll`` and its
sibling dismissal ``POST /inventory/spools/{id}/fresh-roll-dismiss``.

``/new-roll`` is ONE operator statement — "the roll on this slot was physically
replaced" — over TWO ledger lanes, chosen from the bound row's own tag-ness rather
than from anything the operator has to classify:

* a TAGLESS row takes ``spool_tagless.apply_fresh_roll`` (archive + mint + rebind),
* a TAGGED row takes ``spool_respool.respool_tag`` (dispose the donor, mint a fresh
  full third-party row on the reused Bambu tag).

``/fresh-roll-dismiss`` carries the opposite answer to the tagless prompt ("same
roll"), which is a dismissal and not a ledger operation. Called directly (like the
inventory-remain endpoint test) so both run in the unit gate.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.api.routes.inventory import (
    FreshRollDismissRequest,
    NewRollRequest,
    dismiss_fresh_roll_prompt,
    record_new_roll,
)
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import spool_respool, spool_tagless

TAG_UID = "AABBCCDD11223344"
TRAY_UUID = "AABBCCDD11223344AABBCCDD11223344"


@pytest.fixture(autouse=True)
def _clean_state():
    spool_tagless._reset_state()
    yield
    spool_tagless._reset_state()


async def _seed(
    db_session,
    printer_id,
    *,
    used=700,
    spent=False,
    material="PETG",
    rgba="000000FF",
    tag_uid=None,
    bind=True,
):
    """A spool row, optionally tagged, optionally bound to printer/AMS0/slot0."""
    spool = Spool(
        material=material,
        rgba=rgba,
        data_origin="rfid_auto" if tag_uid else "ams_auto",
        tag_uid=tag_uid,
        tray_uuid=TRAY_UUID if tag_uid else None,
        tag_type="bambulab" if tag_uid else None,
        label_weight=1000,
        weight_used=float(used),
        spent_at=datetime.utcnow() if spent else None,
    )
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()
    if bind:
        db_session.add(
            SpoolAssignment(
                spool_id=spool.id,
                printer_id=printer_id,
                ams_id=0,
                tray_id=0,
                fingerprint_color=rgba,
                fingerprint_type=material,
            )
        )
    await db_session.commit()
    return spool


def _live_state(*, tagged=False):
    tray = {"id": 0, "state": 11, "tray_type": "PETG", "tray_color": "000000FF", "tray_info_idx": ""}
    if tagged:
        tray |= {"tag_uid": TAG_UID, "tray_uuid": TRAY_UUID, "tray_weight": "1000", "remain": 100}
    return SimpleNamespace(raw_data={"ams": [{"id": 0, "tray": [tray]}]})


async def _assignment_spool_id(db_session, printer_id):
    res = await db_session.execute(
        select(SpoolAssignment).where(
            SpoolAssignment.printer_id == printer_id, SpoolAssignment.ams_id == 0, SpoolAssignment.tray_id == 0
        )
    )
    return res.scalar_one().spool_id


def _no_spoolman(monkeypatch):
    async def fake_get_setting(db, key):
        return None  # -> schema default tagless filament (feature on), Spoolman off

    monkeypatch.setattr("backend.app.api.routes.settings.get_setting", fake_get_setting)


class TestNewRollTaglessLane:
    async def test_tagless_row_archives_mints_rebinds_and_events(self, db_session, printer_factory, monkeypatch):
        ws = AsyncMock()
        monkeypatch.setattr(spool_tagless.ws_manager, "broadcast", ws)
        _no_spoolman(monkeypatch)
        monkeypatch.setattr(
            "backend.app.api.routes.inventory.apply_spool_to_slot_via_mqtt", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: _live_state())
        monkeypatch.setattr(spool_tagless.printer_manager, "get_client", lambda pid: None)

        printer = await printer_factory()
        spool = await _seed(db_session, printer.id)  # black PETG fingerprint-matches the schema default
        spool.fresh_prompt_pending_at = datetime.utcnow()
        await db_session.commit()

        req = NewRollRequest(
            printer_id=printer.id,
            ams_id=0,
            tray_id=0,
            brand="Jayo",
            label_weight=800,
            cost_per_kg=18.5,
            note="lot 7",
        )
        result = await record_new_roll(spool.id, req, db=db_session, _=None)

        assert result.id != spool.id  # a NEW spool
        assert result.brand == "Jayo"  # optional fields ride the new row
        assert result.label_weight == 800
        assert result.cost_per_kg == 18.5
        assert result.note == "lot 7"
        await db_session.refresh(spool)
        assert spool.archived_at is not None  # old row archived (grams preserved)
        assert await _assignment_spool_id(db_session, printer.id) == result.id  # rebound to the new row
        assert spool.fresh_prompt_pending_at is None  # departed row's prompt answered
        assert result.fresh_prompt_pending_at is None  # the replacement starts unasked
        types = [c.args[0]["type"] for c in ws.await_args_list]
        assert "spool_auto_assigned" in types
        assert "inventory_changed" in types
        assert "tagless_fresh_prompt_dismissed" in types

    async def test_tagless_row_needs_no_brand(self, db_session, printer_factory, monkeypatch):
        """The tagless mint falls back to the configured default — only the TAGGED lane
        needs a brand, so requiring one here would block the common answer."""
        monkeypatch.setattr(spool_tagless.ws_manager, "broadcast", AsyncMock())
        _no_spoolman(monkeypatch)
        monkeypatch.setattr(
            "backend.app.api.routes.inventory.apply_spool_to_slot_via_mqtt", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: _live_state())
        monkeypatch.setattr(spool_tagless.printer_manager, "get_client", lambda pid: None)

        printer = await printer_factory()
        spool = await _seed(db_session, printer.id)

        req = NewRollRequest(printer_id=printer.id, ams_id=0, tray_id=0)
        result = await record_new_roll(spool.id, req, db=db_session, _=None)
        assert result.id != spool.id

    async def test_unresolvable_live_tray_409(self, db_session, printer_factory, monkeypatch):
        _no_spoolman(monkeypatch)
        monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: None)  # printer gone
        printer = await printer_factory()
        spool = await _seed(db_session, printer.id)
        req = NewRollRequest(printer_id=printer.id, ams_id=0, tray_id=0)
        with pytest.raises(HTTPException) as exc:
            await record_new_roll(spool.id, req, db=db_session, _=None)
        assert exc.value.status_code == 409


class TestNewRollTaggedLane:
    async def test_tagged_row_takes_the_respool_lane(self, db_session, printer_factory, monkeypatch):
        """Tag-ness is read off the bound row, never asked of the operator: a row carrying
        an RFID identity routes to the re-spool successor lane with the form's fields."""
        _no_spoolman(monkeypatch)
        printer = await printer_factory()
        donor = await _seed(db_session, printer.id, tag_uid=TAG_UID)
        successor = await _seed(db_session, printer.id, used=0, bind=False)

        calls: list[dict] = []

        async def fake_respool_tag(db, **kwargs):
            calls.append(kwargs)
            return successor

        monkeypatch.setattr(spool_respool, "respool_tag", fake_respool_tag)

        req = NewRollRequest(
            printer_id=printer.id,
            ams_id=0,
            tray_id=0,
            brand="Polymaker",
            label_weight=1000,
            cost_per_kg=22.0,
            note="reused tag",
        )
        result = await record_new_roll(donor.id, req, db=db_session, _=None)

        assert result.id == successor.id
        assert calls == [
            {
                "printer_id": printer.id,
                "ams_id": 0,
                "tray_id": 0,
                "brand": "Polymaker",
                "label_weight": 1000,
                "cost_per_kg": 22.0,
                "note": "reused tag",
            }
        ]

    async def test_tagged_row_without_brand_422(self, db_session, printer_factory, monkeypatch):
        _no_spoolman(monkeypatch)
        printer = await printer_factory()
        donor = await _seed(db_session, printer.id, tag_uid=TAG_UID)
        req = NewRollRequest(printer_id=printer.id, ams_id=0, tray_id=0, brand="   ")
        with pytest.raises(HTTPException) as exc:
            await record_new_roll(donor.id, req, db=db_session, _=None)
        assert exc.value.status_code == 422

    async def test_tagged_row_in_spoolman_mode_409(self, db_session, printer_factory, monkeypatch):
        async def fake_get_setting(db, key):
            return "true" if key == "spoolman_enabled" else None

        monkeypatch.setattr("backend.app.api.routes.settings.get_setting", fake_get_setting)
        printer = await printer_factory()
        donor = await _seed(db_session, printer.id, tag_uid=TAG_UID)
        req = NewRollRequest(printer_id=printer.id, ams_id=0, tray_id=0, brand="Polymaker")
        with pytest.raises(HTTPException) as exc:
            await record_new_roll(donor.id, req, db=db_session, _=None)
        assert exc.value.status_code == 409
        assert "Spoolman" in exc.value.detail

    async def test_respool_error_is_mapped_to_its_own_status(self, db_session, printer_factory, monkeypatch):
        _no_spoolman(monkeypatch)
        printer = await printer_factory()
        donor = await _seed(db_session, printer.id, tag_uid=TAG_UID)

        async def fake_respool_tag(db, **kwargs):
            raise spool_respool.RespoolError(400, "Slot holds no readable tag")

        monkeypatch.setattr(spool_respool, "respool_tag", fake_respool_tag)

        req = NewRollRequest(printer_id=printer.id, ams_id=0, tray_id=0, brand="Polymaker")
        with pytest.raises(HTTPException) as exc:
            await record_new_roll(donor.id, req, db=db_session, _=None)
        assert exc.value.status_code == 400
        assert exc.value.detail == "Slot holds no readable tag"


class TestNewRollGuards:
    async def test_unknown_spool_404(self, db_session):
        req = NewRollRequest(printer_id=1, ams_id=0, tray_id=0)
        with pytest.raises(HTTPException) as exc:
            await record_new_roll(999999, req, db=db_session, _=None)
        assert exc.value.status_code == 404

    async def test_row_not_bound_to_the_named_slot_409(self, db_session, printer_factory):
        """The verb is keyed by the BOUND row: an unbound row cannot have been swapped out
        of the slot, and acting anyway would bind a successor where nothing was retired."""
        printer = await printer_factory()
        spool = await _seed(db_session, printer.id, bind=False)
        req = NewRollRequest(printer_id=printer.id, ams_id=0, tray_id=0)
        with pytest.raises(HTTPException) as exc:
            await record_new_roll(spool.id, req, db=db_session, _=None)
        assert exc.value.status_code == 409

    async def test_row_bound_to_a_different_slot_409(self, db_session, printer_factory):
        printer = await printer_factory()
        spool = await _seed(db_session, printer.id)  # bound to AMS0 slot0
        req = NewRollRequest(printer_id=printer.id, ams_id=0, tray_id=2)
        with pytest.raises(HTTPException) as exc:
            await record_new_roll(spool.id, req, db=db_session, _=None)
        assert exc.value.status_code == 409


class TestFreshRollDismissRoute:
    async def test_same_roll_clears_and_dismisses_no_permanent_stamp(self, db_session, printer_factory, monkeypatch):
        ws = AsyncMock()
        monkeypatch.setattr(spool_tagless.ws_manager, "broadcast", ws)
        printer = await printer_factory()
        spool = await _seed(db_session, printer.id)
        spool.fresh_prompt_pending_at = datetime.utcnow()  # the prompt is outstanding
        await db_session.commit()

        req = FreshRollDismissRequest(printer_id=printer.id, ams_id=0, tray_id=0)
        result = await dismiss_fresh_roll_prompt(spool.id, req, db=db_session, _=None)

        assert result.id == spool.id  # same row returned, unchanged
        assert spool.fresh_prompt_pending_at is None  # durable per-cycle stamp cleared
        types = [c.args[0]["type"] for c in ws.await_args_list]
        assert "tagless_fresh_prompt_dismissed" in types
        await db_session.refresh(spool)
        assert spool.respool_dismissed_at is None  # NO permanent stamp for tagless prompts

    async def test_unknown_spool_404(self, db_session):
        req = FreshRollDismissRequest(printer_id=1, ams_id=0, tray_id=0)
        with pytest.raises(HTTPException) as exc:
            await dismiss_fresh_roll_prompt(999999, req, db=db_session, _=None)
        assert exc.value.status_code == 404
