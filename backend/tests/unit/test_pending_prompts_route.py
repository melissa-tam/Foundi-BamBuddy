"""Tests for GET /inventory/prompts/pending — the REST fallback for slot prompts.

Both prompt events are fire-once websocket broadcasts: ``ws_manager.broadcast`` reaches
only the sockets connected at emit time and no-ops entirely on an empty list, so a
prompt raised while the UI was closed reached nobody (2026-07-24: a tagless fresh-roll
prompt fired at 19:39 to zero clients, a 21:20 restart wiped the in-memory set, and the
reconnect replay then had nothing to replay). The websocket replay covers a RE-connect;
this route covers a client with no reconnect to ride.

Called directly (like ``test_tagless_fresh_route``) so it runs in the unit gate.
"""

import inspect
from datetime import datetime
from types import SimpleNamespace

import pytest

from backend.app.api.routes.inventory import list_pending_slot_prompts, list_spools
from backend.app.core.permissions import Permission
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import spool_respool, spool_tagless

_DONOR_TAG_UID = "A1B2C3D4E5F60718"
_DONOR_TRAY_UUID = "F" * 32


@pytest.fixture(autouse=True)
def _clean_state():
    spool_tagless._reset_state()
    spool_respool._respool_prompt_dedup.clear()
    yield
    spool_tagless._reset_state()
    spool_respool._respool_prompt_dedup.clear()


def _tagless_tray():
    return {"id": 0, "state": 11, "tray_type": "PETG", "tray_color": "112233FF", "tray_info_idx": ""}


def _tagged_tray():
    return {
        "id": 1,
        "state": 11,
        "tray_type": "PETG",
        "tray_sub_brands": "PETG HF",
        "tray_color": "00FF00FF",
        "tray_info_idx": "GFG02",
        "tag_uid": _DONOR_TAG_UID,
        "tray_uuid": _DONOR_TRAY_UUID,
        "tray_weight": "1000",
        "remain": 100,
    }


def _live_state():
    return SimpleNamespace(raw_data={"ams": [{"id": 0, "tray": [_tagless_tray(), _tagged_tray()]}]})


async def _seed_stamped_tagless(db_session, printer_id):
    """A tagless roll with an OUTSTANDING fresh-roll prompt (the durable stamp)."""
    spool = Spool(
        material="PETG",
        rgba="112233FF",
        data_origin="ams_auto",
        label_weight=1000,
        weight_used=750.0,
        fresh_prompt_pending_at=datetime.utcnow(),
    )
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()
    db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer_id, ams_id=0, tray_id=0))
    await db_session.commit()
    return spool


async def _seed_respool_prompt(db_session, printer_id):
    """A near-empty RFID donor plus the in-memory dedup entry the live gate writes."""
    donor = Spool(
        material="PETG",
        subtype="HF",
        rgba="00FF00FF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
        weight_used=990.0,
        tag_uid=_DONOR_TAG_UID,
        tray_uuid=_DONOR_TRAY_UUID,
        data_origin="rfid_auto",
        tag_type="bambulab",
    )
    donor.k_profiles = []
    donor.assignments = []
    db_session.add(donor)
    await db_session.commit()
    spool_respool._respool_prompt_dedup[printer_id] = {(0, 1): (_DONOR_TAG_UID, _DONOR_TRAY_UUID)}
    return donor


class TestPendingPromptsRoute:
    async def test_returns_both_lists_with_the_websocket_payload_shapes(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        fresh_spool = await _seed_stamped_tagless(db_session, printer.id)
        donor = await _seed_respool_prompt(db_session, printer.id)
        monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: _live_state())
        monkeypatch.setattr(
            "backend.app.services.printer_manager.printer_manager.get_status", lambda pid: _live_state()
        )

        out = await list_pending_slot_prompts(db=db_session, _=None)

        assert set(out) == {"fresh", "respool"}
        assert len(out["fresh"]) == 1 and len(out["respool"]) == 1
        # Byte-identical to what the websocket sends — one contract, two transports.
        assert out["fresh"][0]["type"] == "tagless_fresh_prompt"
        assert out["fresh"][0]["spool_id"] == fresh_spool.id
        assert (out["fresh"][0]["printer_id"], out["fresh"][0]["ams_id"], out["fresh"][0]["tray_id"]) == (
            printer.id,
            0,
            0,
        )
        assert out["respool"][0]["type"] == "respool_prompt"
        assert out["respool"][0]["donor_spool_id"] == donor.id

    async def test_matches_the_websocket_replay_exactly(self, db_session, printer_factory, monkeypatch):
        """The route and the reconnect replay read the SAME snapshot builders, so they
        can never disagree about what is outstanding."""
        printer = await printer_factory()
        await _seed_stamped_tagless(db_session, printer.id)
        await _seed_respool_prompt(db_session, printer.id)
        monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: _live_state())
        monkeypatch.setattr(
            "backend.app.services.printer_manager.printer_manager.get_status", lambda pid: _live_state()
        )

        replayed: list[dict] = []

        async def send(payload):
            replayed.append(payload)

        await spool_tagless.rebroadcast_unresolved_tagless_prompts(db_session, send)
        await spool_respool.rebroadcast_unresolved_respool_prompts(db_session, send)

        out = await list_pending_slot_prompts(db=db_session, _=None)
        assert out["fresh"] + out["respool"] == replayed

    async def test_empty_when_nothing_outstanding(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        spool = await _seed_stamped_tagless(db_session, printer.id)
        spool.fresh_prompt_pending_at = None  # answered
        await db_session.commit()
        monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: _live_state())

        assert await list_pending_slot_prompts(db=db_session, _=None) == {"fresh": [], "respool": []}

    def test_guarded_by_the_same_permission_as_sibling_inventory_gets(self):
        """Structural: the route carries the inventory-read dependency, exactly like
        ``GET /inventory/spools`` — a prompt list names spools, printers and slots."""

        def _permissions(fn):
            dep = inspect.signature(fn).parameters["_"].default.dependency
            cells = dict(zip(dep.__code__.co_freevars, (c.cell_contents for c in dep.__closure__), strict=True))
            return cells["perm_strings"]

        assert _permissions(list_pending_slot_prompts) == [Permission.INVENTORY_READ.value]
        assert _permissions(list_pending_slot_prompts) == _permissions(list_spools)
