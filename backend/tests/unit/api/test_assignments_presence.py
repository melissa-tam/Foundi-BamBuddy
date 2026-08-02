"""GET /inventory/assignments — the additive tri-state ``present`` field (W4).

An assignment row is a LOCATION CLAIM, not evidence that a roll is in the slot.
Five prod slots proved the gap (003-T2 spool 140 at 932 g bound to a tray the
operator had emptied): every UI surface rendered a different answer because none
of them could see the wire. The row now carries the ONE presence rule's verdict —
derived per request, never stored — so a client can tell "empty" from "unknown"
instead of guessing.
"""

from types import SimpleNamespace

import pytest

from backend.app.api.routes.inventory import list_assignments
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment

pytestmark = pytest.mark.asyncio


class _FakeManager:
    def __init__(self, states: dict):
        self._states = states

    def get_all_statuses(self):
        return self._states


def _status(trays: list[dict], *, ams_id: int = 0):
    return SimpleNamespace(raw_data={"ams": [{"id": ams_id, "tray": trays}]})


async def _bind(db, printer_id: int, ams_id: int, tray_id: int) -> Spool:
    spool = Spool(material="PETG", rgba="000000FF", label_weight=1000, weight_used=0)
    db.add(spool)
    await db.flush()
    db.add(SpoolAssignment(spool_id=spool.id, printer_id=printer_id, ams_id=ams_id, tray_id=tray_id))
    await db.commit()
    return spool


async def _rows(db, printer, states, monkeypatch):
    monkeypatch.setattr(
        "backend.app.services.printer_manager.printer_manager",
        _FakeManager({printer.id: states} if states is not None else {}),
    )
    return await list_assignments(printer_id=printer.id, db=db, _=None)


async def test_tri_state_present_per_slot(db_session, printer_factory, monkeypatch):
    """One live status, three slots, three honest answers."""
    printer = await printer_factory()
    await _bind(db_session, printer.id, 0, 0)  # seated
    await _bind(db_session, printer.id, 0, 1)  # cleared-tray shape
    await _bind(db_session, printer.id, 0, 2)  # A1-family dialect → unknown

    rows = await _rows(
        db_session,
        printer,
        _status(
            [
                {"id": 0, "state": 11, "tray_type": "PETG", "remain": 80},
                {"id": 1, "state": 9, "tray_type": "", "remain": 0},
                {"id": 2, "state": 3, "tray_type": "PLA", "remain": 50},
            ]
        ),
        monkeypatch,
    )

    by_tray = {r.tray_id: r.present for r in rows}
    assert by_tray == {0: True, 1: False, 2: None}


async def test_offline_printer_reports_unknown_not_empty(db_session, printer_factory, monkeypatch):
    """No live status is NO evidence. Answering False here would tell every consumer
    that gates on ``is False`` to treat a disconnected printer's slots as empty."""
    printer = await printer_factory()
    await _bind(db_session, printer.id, 0, 0)

    rows = await _rows(db_session, printer, None, monkeypatch)

    assert [r.present for r in rows] == [None]


async def test_slot_the_push_never_mentions_is_unknown(db_session, printer_factory, monkeypatch):
    """Silence about a slot is not an observation of it."""
    printer = await printer_factory()
    await _bind(db_session, printer.id, 1, 3)  # bound to a unit the status omits

    rows = await _rows(db_session, printer, _status([{"id": 0, "state": 11, "tray_type": "PETG"}]), monkeypatch)

    assert [r.present for r in rows] == [None]


async def test_field_is_additive_and_defaults_to_none(db_session, printer_factory, monkeypatch):
    """Pre-existing fields are untouched; ``present`` defaults to None so older
    clients and fixtures are unaffected."""
    printer = await printer_factory()
    spool = await _bind(db_session, printer.id, 0, 0)

    rows = await _rows(db_session, printer, _status([{"id": 0, "state": 10}]), monkeypatch)

    assert len(rows) == 1
    assert rows[0].spool_id == spool.id
    assert rows[0].printer_id == printer.id
    assert rows[0].present is True
