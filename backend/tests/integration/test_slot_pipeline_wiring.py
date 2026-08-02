"""The W3b wiring: RAW AMS push → MQTT client hook → printer_manager → slot pipeline.

The pipeline's decisions are pinned by ``test_slot_pipeline.py`` and the table by
``test_slot_state.py``. What is pinned HERE is the thing neither of those can see: that
a real :class:`BambuMQTTClient` push actually reaches the pipeline, and reaches it with
the RAW pre-merge view of the tray rather than the merged display state.

That distinction is the whole reason the hook exists. ``_handle_ams_data``'s merge
deliberately never clears an identity field, so once a slot has reported a
``tray_uuid`` the merged tray keeps it forever — pair that with a DIFFERENT roll's
``tag_uid`` on a later push and the merged dict is a chimera (001-H2S T3, 2026-08-01:
820 g charged to a roll that had left the fleet). Feeding the pipeline the raw push is
what makes the observation layer's atomic-pair rule mean anything.

No MQTT broker is involved: ``connect`` is stubbed and ``_handle_ams_data`` is invoked
directly with the payload shape the firmware sends.
"""

import asyncio
import inspect
import logging
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import slot_pipeline, spool_binding, spool_tagless
from backend.app.services.printer_manager import printer_manager

_PIPELINE_LOGGER = "backend.app.services.slot_pipeline"

_TAG_A = "AABBCCDD00000100"
_TAG_B = "1122334400000100"  # the same roll's OTHER factory chip, or a different roll
_UUID_A = "8AC9EC0847FD41D0890870319F2E1975"


def _tray(**overrides) -> dict:
    tray = {
        "id": 0,
        "state": 11,
        "tray_type": "PETG",
        "tray_color": "000000FF",
        "tray_info_idx": "GFG02",
        "tray_sub_brands": "PETG HF",
        "remain": 100,
    }
    tray.update(overrides)
    return tray


def _push(tray: dict) -> dict:
    """One AMS push in the firmware's dict shape (unit list + presence bitmask)."""
    return {"ams": [{"id": 0, "tray": [tray]}], "tray_exist_bits": "1"}


@pytest.fixture
async def wired_client(printer_factory):
    """A real BambuMQTTClient wired by ``printer_manager.connect_printer`` — the
    production wiring, with only the socket stubbed out."""
    printer = await printer_factory(name="H2S")
    # Held as a plain int, never as an ORM attribute: ``_settle`` expires this session
    # on every poll to see the pass's writes, so any instance it holds goes stale.
    printer_id = printer.id

    slot_pipeline._reset_state()
    spool_tagless._reset_state()
    spool_binding._move_damper.reset()

    previous_loop = printer_manager._loop
    previous_ams_cb = printer_manager._on_ams_change
    printer_manager.set_event_loop(asyncio.get_running_loop())
    # The merged-state consumer lane is main.on_ams_change's business and is exercised
    # by its own tests; detach it so this test observes the raw lane alone.
    printer_manager._on_ams_change = None

    with patch("backend.app.services.bambu_mqtt.BambuMQTTClient.connect", return_value=None):
        await printer_manager.connect_printer(printer)

    client = printer_manager.get_client(printer_id)
    try:
        yield printer_id, client
    finally:
        printer_manager._clients.pop(printer_id, None)
        printer_manager._on_ams_change = previous_ams_cb
        printer_manager._loop = previous_loop
        slot_pipeline._reset_state()


async def _settle(db_session: AsyncSession, predicate, *, tries: int = 60):
    """Let the fire-and-forget pass run, then re-read committed state.

    The wrapper schedules the pipeline onto the loop rather than awaiting it (it is
    called from the MQTT thread in production), so a test must yield until the pass has
    landed. ``expire_all`` (never ``rollback``) is what makes this session re-read:
    both sessions ride one SQLite connection in the test harness, so rolling back here
    would discard the pipeline's own in-flight INSERT.
    """
    for _ in range(tries):
        await asyncio.sleep(0.02)
        db_session.expire_all()
        value = predicate()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
    return None


async def _assignment(db_session: AsyncSession, printer_id: int) -> SpoolAssignment | None:
    res = await db_session.execute(
        select(SpoolAssignment).where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == 0,
            SpoolAssignment.tray_id == 0,
        )
    )
    return res.scalar_one_or_none()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_raw_push_with_a_new_tag_ends_with_a_bound_row(
    async_client: AsyncClient, wired_client, db_session: AsyncSession
):
    """The wiring end to end: an unknown full identity pair on an unbound slot must
    come out the other side as an inventory row bound to that slot."""
    printer_id, client = wired_client

    client._handle_ams_data(_push(_tray(tag_uid=_TAG_A, tray_uuid=_UUID_A)))

    assignment = await _settle(db_session, lambda: _assignment(db_session, printer_id))
    assert assignment is not None, "the raw push never reached the pipeline"

    spool = await db_session.get(Spool, assignment.spool_id)
    assert spool.tag_uid.upper() == _TAG_A
    assert spool.tray_uuid.upper() == _UUID_A
    # The binding's fingerprint is the slot's snapshot, so the next push reads as a match.
    assert assignment.fingerprint_type == "PETG"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_pipeline_sees_the_raw_push_not_the_merged_chimera(
    async_client: AsyncClient, wired_client, db_session: AsyncSession, caplog
):
    """The merge stays DISPLAY-only.

    Push 1 establishes tag A + uuid U. Push 2 is a partial that asserts only tag B —
    the shape a sibling-chip read produces. The MERGED tray is now a chimera (tag B
    beside the retained uuid U); had that reached the table, the uuid would have
    "agreed" and the slot would have been silently KEPT for a roll it may no longer
    hold. From the RAW push the pipeline sees a tag-only disagreement, which proves
    nothing either way, and must DEFER for a full read instead of guessing.
    """
    printer_id, client = wired_client

    client._handle_ams_data(_push(_tray(tag_uid=_TAG_A, tray_uuid=_UUID_A)))
    assignment = await _settle(db_session, lambda: _assignment(db_session, printer_id))
    assert assignment is not None
    bound_spool_id = assignment.spool_id
    spools_before = await db_session.scalar(select(func.count(Spool.id)))

    with caplog.at_level(logging.DEBUG, logger=_PIPELINE_LOGGER):
        # A partial push: tag asserted, uuid NOT carried by this frame.
        client._handle_ams_data(_push(_tray(tag_uid=_TAG_B)))
        await _settle(db_session, lambda: _saw_defer(caplog))

    # The merged display state IS the chimera — that is by design, and why it must not
    # drive a binding decision.
    merged_tray = client.state.raw_data["ams"][0]["tray"][0]
    assert merged_tray["tag_uid"].upper() == _TAG_B
    assert merged_tray["tray_uuid"].upper() == _UUID_A  # never cleared by the merge

    # ...and the pipeline, fed the RAW frame, refused to decide on it.
    assert _saw_defer(caplog), "expected an owed-full-read defer, not a decision"
    after = await _assignment(db_session, printer_id)
    assert after is not None and after.spool_id == bound_spool_id  # binding untouched
    assert await db_session.scalar(select(func.count(Spool.id))) == spools_before  # no twin row


def _saw_defer(caplog) -> bool:
    return any("identity_ambiguous_owed_full_read" in r.getMessage() for r in caplog.records)
