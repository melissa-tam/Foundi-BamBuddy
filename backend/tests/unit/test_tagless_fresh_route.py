"""Tests for POST /inventory/spools/{id}/tagless-fresh (W5).

The route answers the tagless fresh-roll prompt. "same" clears the per-cycle prompt
(broadcasts the dismissed event, NO permanent respool_dismissed_at stamp); "fresh"
archives the current tagless row and mints + binds + pushes a replacement with the
operator's optional brand/label_weight/cost_per_kg/note. Called directly (like the
inventory-remain endpoint test) so it runs in the unit gate.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.api.routes.inventory import TaglessFreshRequest, answer_tagless_fresh
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import spool_tagless


@pytest.fixture(autouse=True)
def _clean_state():
    spool_tagless._reset_state()
    yield
    spool_tagless._reset_state()


async def _seed(db_session, printer_id, *, used=700, spent=False, material="PETG", rgba="000000FF"):
    spool = Spool(
        material=material,
        rgba=rgba,
        data_origin="ams_auto",
        label_weight=1000,
        weight_used=float(used),
        spent_at=datetime.utcnow() if spent else None,
    )
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()
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


def _live_state():
    return SimpleNamespace(
        raw_data={
            "ams": [
                {
                    "id": 0,
                    "tray": [
                        {"id": 0, "state": 11, "tray_type": "PETG", "tray_color": "000000FF", "tray_info_idx": ""}
                    ],
                }
            ]
        }
    )


async def _assignment_spool_id(db_session, printer_id):
    res = await db_session.execute(
        select(SpoolAssignment).where(
            SpoolAssignment.printer_id == printer_id, SpoolAssignment.ams_id == 0, SpoolAssignment.tray_id == 0
        )
    )
    sa = res.scalar_one()
    return sa.spool_id


class TestTaglessFreshRoute:
    async def test_same_clears_and_dismisses_no_permanent_stamp(self, db_session, printer_factory, monkeypatch):
        ws = AsyncMock()
        monkeypatch.setattr(spool_tagless.ws_manager, "broadcast", ws)
        printer = await printer_factory()
        spool = await _seed(db_session, printer.id)
        spool_tagless._fresh_prompt_unanswered.add((printer.id, 0, 0))

        req = TaglessFreshRequest(printer_id=printer.id, ams_id=0, tray_id=0, answer="same")
        result = await answer_tagless_fresh(spool.id, req, db=db_session, _=None)

        assert result.id == spool.id  # same row returned, unchanged
        assert (printer.id, 0, 0) not in spool_tagless._fresh_prompt_unanswered  # per-cycle entry cleared
        types = [c.args[0]["type"] for c in ws.await_args_list]
        assert "tagless_fresh_prompt_dismissed" in types
        await db_session.refresh(spool)
        assert spool.respool_dismissed_at is None  # NO permanent stamp for tagless prompts

    async def test_fresh_archives_mints_rebinds_and_events(self, db_session, printer_factory, monkeypatch):
        ws = AsyncMock()
        monkeypatch.setattr(spool_tagless.ws_manager, "broadcast", ws)

        async def fake_get_setting(db, key):
            return None  # -> schema default tagless filament (feature on)

        monkeypatch.setattr("backend.app.api.routes.settings.get_setting", fake_get_setting)
        monkeypatch.setattr(
            "backend.app.api.routes.inventory.apply_spool_to_slot_via_mqtt", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: _live_state())
        monkeypatch.setattr(spool_tagless.printer_manager, "get_client", lambda pid: None)

        printer = await printer_factory()
        spool = await _seed(db_session, printer.id)  # black PETG fingerprint-matches the schema default
        spool_tagless._fresh_prompt_unanswered.add((printer.id, 0, 0))

        req = TaglessFreshRequest(
            printer_id=printer.id,
            ams_id=0,
            tray_id=0,
            answer="fresh",
            brand="Jayo",
            label_weight=800,
            cost_per_kg=18.5,
            note="lot 7",
        )
        result = await answer_tagless_fresh(spool.id, req, db=db_session, _=None)

        assert result.id != spool.id  # a NEW spool
        assert result.brand == "Jayo"  # optional fields ride the new row
        assert result.label_weight == 800
        assert result.cost_per_kg == 18.5
        assert result.note == "lot 7"
        await db_session.refresh(spool)
        assert spool.archived_at is not None  # old row archived (grams preserved)
        assert await _assignment_spool_id(db_session, printer.id) == result.id  # rebound to the new row
        assert (printer.id, 0, 0) not in spool_tagless._fresh_prompt_unanswered
        types = [c.args[0]["type"] for c in ws.await_args_list]
        assert "spool_auto_assigned" in types
        assert "inventory_changed" in types
        assert "tagless_fresh_prompt_dismissed" in types

    async def test_unknown_spool_404(self, db_session):
        req = TaglessFreshRequest(printer_id=1, ams_id=0, tray_id=0, answer="same")
        with pytest.raises(HTTPException) as exc:
            await answer_tagless_fresh(999999, req, db=db_session, _=None)
        assert exc.value.status_code == 404

    async def test_invalid_answer_422(self, db_session, printer_factory):
        printer = await printer_factory()
        spool = await _seed(db_session, printer.id)
        req = TaglessFreshRequest(printer_id=printer.id, ams_id=0, tray_id=0, answer="bogus")
        with pytest.raises(HTTPException) as exc:
            await answer_tagless_fresh(spool.id, req, db=db_session, _=None)
        assert exc.value.status_code == 422

    async def test_fresh_no_live_tray_409(self, db_session, printer_factory, monkeypatch):
        async def fake_get_setting(db, key):
            return None

        monkeypatch.setattr("backend.app.api.routes.settings.get_setting", fake_get_setting)
        monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: None)  # printer gone
        printer = await printer_factory()
        spool = await _seed(db_session, printer.id)
        req = TaglessFreshRequest(printer_id=printer.id, ams_id=0, tray_id=0, answer="fresh")
        with pytest.raises(HTTPException) as exc:
            await answer_tagless_fresh(spool.id, req, db=db_session, _=None)
        assert exc.value.status_code == 409
