"""The label weight a new spool row is minted with — ``services/spool_defaults`` (Part 2, 2026-10-16).

Before it, 163 untagged rolls minted since 2026-10-08 all carried a 1000 g literal, and the main AMS
lane dropped a tag's own ``tray_weight``, so RFID rolls minted at the literal too. The owner's rule is
"a positive stated figure wins, else the ``default_spool_weight_g`` setting". These tests drive each
way a roll is created through its public entry, against the REAL settings table (the owner's one read),
and assert the minted row's label:

* the owner itself (unset / set / unreadable / out of range; the rule's "states nothing" cases);
* a raw AMS push through the full pipeline (``observe_ams_push`` → ``run_slot_pipeline``): a tag that
  states 750 mints 750, a tag stating ``"0"`` or nothing falls to the setting, an untagged tray mints at
  the setting;
* the bare-tray autoconfig (the tagless default mint);
* the operator's New roll on a tagless row (the replacement), with and without a figure;
* Add spool (``POST /inventory/spools`` and ``/bulk`` handlers), with and without a figure;
* CSV import (``parse_and_validate``) with an empty, a zero and a stated cell.

The respool lanes are pinned beside their own suite (``test_spool_respool.py``); the settings round-trip
and the HTTP surface in ``tests/integration/test_default_spool_weight.py``.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.app.api.routes.inventory import NewRollRequest, bulk_create_spools, create_spool, record_new_roll
from backend.app.api.routes.settings import get_setting, set_setting
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.schemas.spool import SpoolBulkCreate, SpoolCreate
from backend.app.services import ams_presence, slot_pipeline, spool_binding, spool_defaults, spool_tagless
from backend.app.services.slot_pipeline import PipelineDeps, run_slot_pipeline
from backend.app.services.spool_csv import parse_and_validate
from backend.app.services.tray_observation import observe_ams_push

TAG = "AABBCCDD11223344"
UUID = "8AC9EC0847FD41D0890870319F2E1975"


@pytest.fixture(autouse=True)
def _clean_state():
    slot_pipeline._reset_state()
    spool_tagless._reset_state()
    ams_presence._reset_state()
    spool_binding._move_damper.reset()
    yield
    slot_pipeline._reset_state()
    spool_tagless._reset_state()
    ams_presence._reset_state()
    spool_binding._move_damper.reset()


@pytest.fixture
def no_wire(monkeypatch):
    """No live printer, no MQTT config push, no websocket — the mint lanes write the DB only."""
    monkeypatch.setattr(spool_tagless.ws_manager, "broadcast", AsyncMock())
    monkeypatch.setattr("backend.app.services.spool_tagless.apply_spool_to_slot_via_mqtt", AsyncMock(return_value=True))
    monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: None)
    monkeypatch.setattr(spool_tagless.printer_manager, "get_client", lambda pid: None)


async def _set_default(db, grams: int) -> None:
    await set_setting(db, spool_defaults.SETTING_KEY, str(grams))
    await db.commit()


def _deps(db) -> PipelineDeps:
    async def _setting(key: str) -> str | None:
        return await get_setting(db, key)

    async def _noop(*_args, **_kwargs):
        return None

    async def _pushed(*_args, **_kwargs) -> bool:
        return True

    return PipelineDeps(db=db, get_setting=_setting, schedule_identify=_noop, broadcast=_noop, push_config=_pushed)


async def _push(db, printer_id: int, tray: dict) -> Spool:
    """One RAW AMS push for slot A0T0, through the observation layer and the pipeline; the row bound."""
    observations = observe_ams_push(printer_id, {"ams": [{"id": 0, "tray": [{"id": 0, **tray}]}]})
    transitions = await run_slot_pipeline(printer_id, observations, _deps(db))
    assert transitions and transitions[0].applied, transitions
    row = (
        await db.execute(
            select(SpoolAssignment).where(
                SpoolAssignment.printer_id == printer_id, SpoolAssignment.ams_id == 0, SpoolAssignment.tray_id == 0
            )
        )
    ).scalar_one()
    return await db.get(Spool, row.spool_id)


def _tagged(**extra) -> dict:
    return {
        "state": 11,
        "tag_uid": TAG,
        "tray_uuid": UUID,
        "tray_type": "PETG",
        "tray_sub_brands": "PETG HF",
        "tray_color": "112233FF",
        "remain": 100,
        **extra,
    }


def _untagged(**extra) -> dict:
    return {"state": 11, "tray_type": "PLA", "tray_color": "FF0000FF", "tray_info_idx": "GFL99", **extra}


# --- the owner -------------------------------------------------------------------------------------


class TestTheOwner:
    async def test_unset_reads_the_schema_default(self, db_session):
        assert await spool_defaults.default_label_weight_g(db_session) == 1000

    async def test_reads_the_setting(self, db_session):
        await _set_default(db_session, 1100)
        assert await spool_defaults.default_label_weight_g(db_session) == 1100

    @pytest.mark.parametrize("raw", ["None", "", "abc", "0", "-5", "49", "5001"])
    async def test_an_unreadable_or_out_of_range_value_reads_the_schema_default(self, db_session, raw):
        await set_setting(db_session, spool_defaults.SETTING_KEY, raw)
        await db_session.commit()
        assert await spool_defaults.default_label_weight_g(db_session) == 1000

    @pytest.mark.parametrize("figure", [None, "0", 0, -3, "", "junk", True])
    async def test_a_figure_that_states_nothing_falls_to_the_setting(self, db_session, figure):
        await _set_default(db_session, 1100)
        assert await spool_defaults.resolve_label_weight(db_session, figure) == 1100
        assert spool_defaults.label_weight_for(figure, default_g=1100) == 1100

    async def test_the_first_positive_figure_wins(self, db_session):
        await _set_default(db_session, 1100)
        assert await spool_defaults.resolve_label_weight(db_session, None, "750", 900) == 750
        assert await spool_defaults.resolve_label_weight(db_session, 0, 900) == 900


# --- a raw AMS push through the full pipeline --------------------------------------------------------


class TestTheAmsLane:
    async def test_a_tag_stating_750_mints_750(self, db_session, printer_factory, no_wire):
        await _set_default(db_session, 1100)
        printer = await printer_factory()
        spool = await _push(db_session, printer.id, _tagged(tray_weight="750"))
        assert spool.data_origin == "rfid_auto"
        assert spool.label_weight == 750

    @pytest.mark.parametrize("weight", ["0", None])
    async def test_a_tag_stating_no_weight_mints_at_the_setting(self, db_session, printer_factory, no_wire, weight):
        await _set_default(db_session, 1100)
        printer = await printer_factory()
        tray = _tagged() if weight is None else _tagged(tray_weight=weight)
        spool = await _push(db_session, printer.id, tray)
        assert spool.data_origin == "rfid_auto"
        assert spool.label_weight == 1100

    async def test_an_untagged_tray_mints_at_the_setting(self, db_session, printer_factory, no_wire):
        await _set_default(db_session, 1100)
        printer = await printer_factory()
        spool = await _push(db_session, printer.id, _untagged(tray_weight="0"))
        assert spool.data_origin == "ams_auto"
        assert spool.label_weight == 1100

    async def test_an_untagged_tray_with_the_setting_unset_mints_at_the_schema_default(
        self, db_session, printer_factory, no_wire
    ):
        printer = await printer_factory()
        spool = await _push(db_session, printer.id, _untagged())
        assert spool.label_weight == 1000


# --- the tagless default mint (bare-tray autoconfig) -------------------------------------------------


class TestTheBareTrayAutoconfig:
    async def test_a_bare_tray_mints_the_default_filament_at_the_setting(self, db_session, printer_factory, no_wire):
        await _set_default(db_session, 1100)
        printer = await printer_factory()
        bare = {
            "id": 0,
            "state": 11,
            "tray_type": "",
            "tray_sub_brands": "",
            "tray_color": "",
            "tray_info_idx": "",
            "tag_uid": "0" * 16,
            "tray_uuid": "0" * 32,
        }
        assert await spool_tagless.maybe_autoconfigure_bare_tray(db_session, printer.id, 0, 0, bare) is True
        row = (
            await db_session.execute(select(SpoolAssignment).where(SpoolAssignment.printer_id == printer.id))
        ).scalar_one()
        spool = await db_session.get(Spool, row.spool_id)
        assert spool.data_origin == "ams_auto"
        assert spool.label_weight == 1100


# --- the New roll replacement (tagless lane) ---------------------------------------------------------


async def _bound_tagless_row(db, printer_id: int) -> Spool:
    spool = Spool(
        material="PETG",
        rgba="000000FF",
        data_origin="ams_auto",
        label_weight=1000,
        weight_used=700.0,
        fresh_prompt_pending_at=datetime.utcnow(),
    )
    spool.k_profiles = []
    spool.assignments = []
    db.add(spool)
    await db.flush()
    db.add(
        SpoolAssignment(
            spool_id=spool.id,
            printer_id=printer_id,
            ams_id=0,
            tray_id=0,
            fingerprint_color="000000FF",
            fingerprint_type="PETG",
        )
    )
    await db.commit()
    return spool


@pytest.fixture
def live_tray(monkeypatch, no_wire):
    tray = {
        "id": 0,
        "state": 11,
        "tray_type": "PETG",
        "tray_color": "000000FF",
        "tray_info_idx": "",
        "tray_weight": "0",
    }
    state = SimpleNamespace(raw_data={"ams": [{"id": 0, "tray": [tray]}]})
    monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: state)


class TestTheNewRollReplacement:
    async def test_no_figure_mints_at_the_setting(self, db_session, printer_factory, live_tray):
        await _set_default(db_session, 1100)
        printer = await printer_factory()
        departed = await _bound_tagless_row(db_session, printer.id)
        new = await record_new_roll(
            departed.id, NewRollRequest(printer_id=printer.id, ams_id=0, tray_id=0), db=db_session, _=None
        )
        assert new.id != departed.id
        assert new.label_weight == 1100

    async def test_the_operator_figure_wins(self, db_session, printer_factory, live_tray):
        await _set_default(db_session, 1100)
        printer = await printer_factory()
        departed = await _bound_tagless_row(db_session, printer.id)
        new = await record_new_roll(
            departed.id,
            NewRollRequest(printer_id=printer.id, ams_id=0, tray_id=0, label_weight=800),
            db=db_session,
            _=None,
        )
        assert new.label_weight == 800

    async def test_the_departed_row_keeps_its_label(self, db_session, printer_factory, live_tray):
        """Existing rows never change: the archived row's label is its own history."""
        await _set_default(db_session, 1100)
        printer = await printer_factory()
        departed = await _bound_tagless_row(db_session, printer.id)
        await record_new_roll(
            departed.id, NewRollRequest(printer_id=printer.id, ams_id=0, tray_id=0), db=db_session, _=None
        )
        await db_session.refresh(departed)
        assert departed.label_weight == 1000


# --- Add spool ---------------------------------------------------------------------------------------


class TestAddSpool:
    async def test_no_figure_adds_at_the_setting(self, db_session):
        await _set_default(db_session, 1100)
        created = await create_spool(SpoolCreate(material="PLA"), db=db_session, _=None)
        assert created.label_weight == 1100

    async def test_a_stated_figure_wins(self, db_session):
        await _set_default(db_session, 1100)
        created = await create_spool(SpoolCreate(material="PLA", label_weight=750), db=db_session, _=None)
        assert created.label_weight == 750

    async def test_bulk_add_with_and_without_a_figure(self, db_session):
        await _set_default(db_session, 1100)
        unset = await bulk_create_spools(
            SpoolBulkCreate(spool=SpoolCreate(material="PLA"), quantity=2), db=db_session, _=None
        )
        stated = await bulk_create_spools(
            SpoolBulkCreate(spool=SpoolCreate(material="PLA", label_weight=500), quantity=1), db=db_session, _=None
        )
        assert [s.label_weight for s in unset] == [1100, 1100]
        assert [s.label_weight for s in stated] == [500]


# --- CSV import --------------------------------------------------------------------------------------


class TestCsvImport:
    async def test_the_label_cell_or_the_setting(self, db_session):
        await _set_default(db_session, 1100)
        text = "material,label_weight,weight_used\nPLA,,100\nPETG,0,\nABS,750,\n"
        preview = await parse_and_validate(text.encode("utf-8"), db_session)
        labels = [(row.status, row.spool["label_weight"] if row.spool else None) for row in preview.rows]
        assert labels == [("valid", 1100), ("valid", 1100), ("valid", 750)]

    async def test_weight_used_is_bounded_by_the_setting_when_the_cell_is_empty(self, db_session):
        await _set_default(db_session, 500)
        preview = await parse_and_validate(b"material,label_weight,weight_used\nPLA,,600\n", db_session)
        assert preview.rows[0].status == "error"
        assert "exceeds label_weight (500)" in (preview.rows[0].reason or "")
