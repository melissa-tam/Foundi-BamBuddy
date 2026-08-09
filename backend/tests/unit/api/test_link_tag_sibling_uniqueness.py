"""``PATCH /inventory/spools/{id}/link-tag`` uniqueness spans BOTH of a roll's chips.

A Bambu roll carries two RFID tags — one per flange — sharing one ``tray_uuid``, and
once the far chip has been sighted it is persisted as ``spool.sibling_tag_uid``. From
that moment the tag is spoken for exactly as firmly as the primary: linking it onto a
second row would put ONE physical roll's identity on TWO ledger rows, which is the
duplicate-identity error this 409 exists to prevent.

Route is called directly (the established pattern for inventory route unit tests, e.g.
``test_bulk_spool_create``).
"""

import pytest
from fastapi import HTTPException

from backend.app.api.routes.inventory import LinkTagRequest, link_tag_to_spool
from backend.app.models.spool import Spool

NEAR_CHIP = "EC96F1E700000100"
FAR_CHIP = "3CF1F3E700000100"
ROLL_UUID = "8AC9EC0847FD41D0890870319F2E1975"
UNRELATED_CHIP = "A5E7210D00000100"


async def _spool(db, **kw):
    spool = Spool(material="PETG", label_weight=1000, core_weight=250, **kw)
    spool.k_profiles = []
    spool.assignments = []
    db.add(spool)
    await db.flush()
    return spool


@pytest.mark.asyncio
async def test_linking_a_recorded_sibling_chip_conflicts(db_session):
    """THE FIX. The chip is another active row's FAR side — same roll, so the same 409
    as its primary. Before the span covered ``sibling_tag_uid`` this silently succeeded
    and minted a second identity for one physical roll."""
    await _spool(db_session, tag_uid=NEAR_CHIP, sibling_tag_uid=FAR_CHIP, tray_uuid=ROLL_UUID)
    target = await _spool(db_session, tag_uid=None)
    await db_session.commit()

    with pytest.raises(HTTPException) as exc:
        await link_tag_to_spool(target.id, LinkTagRequest(tag_uid=FAR_CHIP), db=db_session)

    assert exc.value.status_code == 409
    assert "already linked to another active spool" in exc.value.detail


@pytest.mark.asyncio
async def test_linking_the_primary_chip_still_conflicts(db_session):
    """The pre-existing guard is untouched by the widening."""
    await _spool(db_session, tag_uid=NEAR_CHIP, sibling_tag_uid=FAR_CHIP)
    target = await _spool(db_session, tag_uid=None)
    await db_session.commit()

    with pytest.raises(HTTPException) as exc:
        await link_tag_to_spool(target.id, LinkTagRequest(tag_uid=NEAR_CHIP), db=db_session)

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_an_unrelated_chip_links_cleanly(db_session):
    """The widening must not turn into "every link 409s" — a genuinely free tag links."""
    await _spool(db_session, tag_uid=NEAR_CHIP, sibling_tag_uid=FAR_CHIP)
    target = await _spool(db_session, tag_uid=None)
    await db_session.commit()

    result = await link_tag_to_spool(target.id, LinkTagRequest(tag_uid=UNRELATED_CHIP), db=db_session)

    assert result is not None
    await db_session.refresh(target)
    assert target.tag_uid == UNRELATED_CHIP


@pytest.mark.asyncio
async def test_an_archived_rows_sibling_is_recycled_not_a_conflict(db_session):
    """Tag recycling already clears a primary off an ARCHIVED row; a sibling must clear
    the same way, or a retired roll's far chip stays pinned forever and the tag can
    never be re-used."""
    from datetime import datetime

    archived = await _spool(db_session, tag_uid=NEAR_CHIP, sibling_tag_uid=FAR_CHIP, archived_at=datetime.utcnow())
    target = await _spool(db_session, tag_uid=None)
    await db_session.commit()

    await link_tag_to_spool(target.id, LinkTagRequest(tag_uid=FAR_CHIP), db=db_session)

    await db_session.refresh(archived)
    await db_session.refresh(target)
    assert archived.sibling_tag_uid is None, "the archived row released the recycled chip"
    assert archived.tag_uid == NEAR_CHIP, "and kept the chip that was NOT recycled"
    assert target.tag_uid == FAR_CHIP
