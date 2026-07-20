"""Tests for spool_tag_matcher service — RFID auto-assign and relationship loading."""

import pytest
from sqlalchemy import inspect

from backend.app.models.color_catalog import ColorCatalogEntry
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services.spool_tag_matcher import (
    auto_assign_spool,
    create_spool_from_tray,
    find_matching_untagged_spool,
    get_spool_by_tag,
    is_bambu_tag,
    is_valid_tag,
    link_tag_to_inventory_spool,
)

# -- helpers -----------------------------------------------------------------

SAMPLE_TRAY = {
    "tray_type": "PLA",
    "tray_sub_brands": "PLA Basic",
    "tray_color": "FFFFFFFF",
    "tray_id_name": "",
    "tag_uid": "AABBCCDD11223344",
    "tray_uuid": "AABBCCDD11223344AABBCCDD11223344",
    "tray_info_idx": "GFL99",
    "nozzle_temp_min": 190,
    "nozzle_temp_max": 230,
    "tray_weight": "1000",
    "remain": 80,
}


def _relationship_is_loaded(obj, attr_name: str) -> bool:
    """Check if a relationship attribute has been eagerly loaded (not lazy)."""
    return attr_name in inspect(obj).dict


# -- is_valid_tag / is_bambu_tag --------------------------------------------


def test_is_valid_tag_with_real_uid():
    assert is_valid_tag("AABBCCDD11223344", "") is True


def test_is_valid_tag_with_real_uuid():
    assert is_valid_tag("", "AABBCCDD11223344AABBCCDD11223344") is True


def test_is_valid_tag_all_zeros():
    assert is_valid_tag("0000000000000000", "00000000000000000000000000000000") is False


def test_is_valid_tag_empty():
    assert is_valid_tag("", "") is False


def test_is_bambu_tag_with_uuid():
    assert is_bambu_tag("", "AABBCCDD11223344AABBCCDD11223344", "") is True


def test_is_bambu_tag_with_uid_and_preset():
    assert is_bambu_tag("AABBCCDD11223344", "", "GFL99") is True


def test_is_bambu_tag_uid_only_no_preset():
    """A tag UID alone (no UUID, no preset) is NOT considered a Bambu tag."""
    assert is_bambu_tag("AABBCCDD11223344", "", "") is False


# -- create_spool_from_tray -------------------------------------------------


@pytest.mark.asyncio
async def test_create_spool_from_tray_basic(db_session):
    """Created spool has correct material and tag fields."""
    spool = await create_spool_from_tray(db_session, SAMPLE_TRAY)
    await db_session.commit()

    assert spool.id is not None
    assert spool.material == "PLA"
    assert spool.brand == "Bambu Lab"
    assert spool.tag_uid == "AABBCCDD11223344"
    assert spool.tray_uuid == "AABBCCDD11223344AABBCCDD11223344"
    assert spool.data_origin == "rfid_auto"


@pytest.mark.asyncio
async def test_create_spool_from_tray_weight_from_remain(db_session):
    """weight_used is calculated from the AMS remain percentage."""
    spool = await create_spool_from_tray(db_session, SAMPLE_TRAY)
    # remain=80 → 20% used → 200g of 1000g
    assert spool.weight_used == 200.0


@pytest.mark.asyncio
async def test_create_spool_from_tray_relationships_loaded(db_session):
    """Both k_profiles and assignments must be eagerly initialized.

    If these are lazy, db.add(SpoolAssignment(spool_id=spool.id)) triggers
    a back_populates lazy load outside the async greenlet → greenlet_spawn error.
    Regression test for #612.
    """
    spool = await create_spool_from_tray(db_session, SAMPLE_TRAY)

    assert _relationship_is_loaded(spool, "k_profiles"), "k_profiles not eagerly initialized"
    assert _relationship_is_loaded(spool, "assignments"), "assignments not eagerly initialized"
    assert spool.k_profiles == []
    assert spool.assignments == []


# -- get_spool_by_tag -------------------------------------------------------


@pytest.mark.asyncio
async def test_get_spool_by_tag_by_uuid(db_session):
    """Look up a spool by tray_uuid."""
    spool = Spool(
        material="PLA",
        tray_uuid="AABBCCDD11223344AABBCCDD11223344",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()

    found = await get_spool_by_tag(db_session, "", "AABBCCDD11223344AABBCCDD11223344")
    assert found is not None
    assert found.id == spool.id


@pytest.mark.asyncio
async def test_get_spool_by_tag_by_uid(db_session):
    """Fall back to tag_uid when tray_uuid doesn't match."""
    spool = Spool(
        material="PETG",
        tag_uid="1122334455667788",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()

    found = await get_spool_by_tag(db_session, "1122334455667788", "")
    assert found is not None
    assert found.id == spool.id


@pytest.mark.asyncio
async def test_get_spool_by_tag_skips_archived(db_session):
    """Archived spools are not returned."""
    from datetime import datetime

    spool = Spool(
        material="PLA",
        tray_uuid="AABBCCDD11223344AABBCCDD11223344",
        label_weight=1000,
        core_weight=250,
        archived_at=datetime.now(),
    )
    db_session.add(spool)
    await db_session.commit()

    found = await get_spool_by_tag(db_session, "", "AABBCCDD11223344AABBCCDD11223344")
    assert found is None


@pytest.mark.asyncio
async def test_get_spool_by_tag_relationships_loaded(db_session):
    """Both k_profiles and assignments must be eagerly loaded.

    Regression test for #612 — without selectinload(Spool.assignments),
    accessing spool.assignments after get_spool_by_tag triggers a lazy load
    in async context → greenlet_spawn error.
    """
    spool = Spool(
        material="PLA",
        tray_uuid="AABBCCDD11223344AABBCCDD11223344",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()
    # Expire to clear in-session state — forces selectinload to actually load
    db_session.expire(spool)

    found = await get_spool_by_tag(db_session, "", "AABBCCDD11223344AABBCCDD11223344")
    assert found is not None
    assert _relationship_is_loaded(found, "k_profiles"), "k_profiles not eagerly loaded"
    assert _relationship_is_loaded(found, "assignments"), "assignments not eagerly loaded"


@pytest.mark.asyncio
async def test_get_spool_by_tag_returns_none_for_zeros(db_session):
    """Zero-value tags return None."""
    found = await get_spool_by_tag(db_session, "0000000000000000", "00000000000000000000000000000000")
    assert found is None


@pytest.mark.asyncio
async def test_get_spool_by_tag_first_char_variance_same_length(db_session):
    """Match spool when scanned tag differs only in first character.

    Handles case where same physical tag reports different first bytes
    across different readers (e.g., "A45012F" stored, "B45012F" scanned).
    Both tags have same length and differ only in first char.
    """
    spool = Spool(
        material="PLA",
        tag_uid="A4501234CCDDEE88",  # First tag variant
        label_weight=1000,
        core_weight=250,
    )
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.commit()

    # Scan with different first character — should still match
    found = await get_spool_by_tag(db_session, "B4501234CCDDEE88", "")
    assert found is not None
    assert found.id == spool.id


@pytest.mark.asyncio
async def test_get_spool_by_tag_first_char_variance_short_uid(db_session):
    """Match spool when 8-char scanned tag differs only in first character.

    Handles short UID (8 char) from 4-byte readers with first-char variance.
    The stored tag is longer (16 char), but the first 8 chars of the stored tag
    should match the scanned 8-char UID with first-char tolerance.
    """
    spool = Spool(
        material="PLA",
        tag_uid="A4501234CCDDEE88",  # 16-char stored tag
        label_weight=1000,
        core_weight=250,
    )
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.commit()

    # Scan with 8-char short UID whose first char differs but remaining 7 match
    # the first 8 chars of the stored tag: stored[:8] = "A4501234",
    # scanned = "B4501234" → first-char variance on short UID
    found = await get_spool_by_tag(db_session, "B4501234", "")
    assert found is not None
    assert found.id == spool.id


@pytest.mark.asyncio
async def test_get_spool_by_tag_short_uid_exact_match_preferred(db_session):
    """Prefer exact match over first-char variance match."""
    # Spool with exact 8-char UID match
    spool_exact = Spool(
        material="PLA",
        tag_uid="B4501234",
        label_weight=1000,
        core_weight=250,
    )
    spool_exact.k_profiles = []
    spool_exact.assignments = []
    db_session.add(spool_exact)

    # Spool that would match via first-char variance
    spool_variance = Spool(
        material="PETG",
        tag_uid="A4501234",
        label_weight=1000,
        core_weight=250,
    )
    spool_variance.k_profiles = []
    spool_variance.assignments = []
    db_session.add(spool_variance)
    await db_session.commit()

    # Exact match should win over variance match
    found = await get_spool_by_tag(db_session, "B4501234", "")
    assert found is not None
    assert found.id == spool_exact.id


@pytest.mark.asyncio
async def test_get_spool_by_tag_no_false_positive_different_suffix(db_session):
    """Don't match tags with different suffixes just because first char varies."""
    spool = Spool(
        material="PLA",
        tag_uid="AABBCCDD11223344",
        label_weight=1000,
        core_weight=250,
    )
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.commit()

    # Scan with different suffix (only first char is same) — should NOT match
    found = await get_spool_by_tag(db_session, "AABBCCDD11223355", "")
    assert found is None, "Should not match when suffix differs"


# -- variance convergence + different-roll guard (printer-3 flap fix) --------


@pytest.mark.asyncio
async def test_variance_match_converges_scanned_identifiers(db_session):
    """A converge=True variance match persists BOTH scanned tag_uid and tray_uuid
    onto the spool, so the next read is an exact match — killing the auto-unlink ⇄
    re-assign reader-variance loop (printer 3 looped all day 2026-07-14)."""
    spool = Spool(
        material="PETG",
        tag_uid="8C0EF4E700000100",
        tray_uuid="BBC7BDD79A66407BB334A9472E3717E6",
        tag_type="bambulab",
        label_weight=1000,
        core_weight=250,
    )
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.commit()

    # Scan: first-char tag variance + an entirely different tray_uuid (reader drift).
    found = await get_spool_by_tag(db_session, "1C0EF4E700000100", "C4A25BF1D9054983A9C2E73EE0CF4D5A", converge=True)
    assert found is not None and found.id == spool.id
    await db_session.commit()
    await db_session.refresh(spool)
    # Stored identifiers converged onto the scanned values.
    assert spool.tag_uid == "1C0EF4E700000100"
    assert spool.tray_uuid == "C4A25BF1D9054983A9C2E73EE0CF4D5A"

    # The next read is now an EXACT tray_uuid match — no variance branch, loop dead.
    again = await get_spool_by_tag(db_session, "1C0EF4E700000100", "C4A25BF1D9054983A9C2E73EE0CF4D5A")
    assert again is not None and again.id == spool.id


@pytest.mark.asyncio
async def test_variance_match_read_only_does_not_converge(db_session):
    """Default (converge=False) callers get the match but NEVER mutate the spool —
    protecting the SpoolBuddy lookup + re-spool donor-resolution read paths."""
    spool = Spool(
        material="PETG",
        tag_uid="8C0EF4E700000100",
        tray_uuid="BBC7BDD79A66407BB334A9472E3717E6",
        tag_type="bambulab",
        label_weight=1000,
        core_weight=250,
    )
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.commit()

    found = await get_spool_by_tag(db_session, "1C0EF4E700000100", "C4A25BF1D9054983A9C2E73EE0CF4D5A")
    assert found is not None and found.id == spool.id
    await db_session.commit()
    await db_session.refresh(spool)
    # Untouched.
    assert spool.tag_uid == "8C0EF4E700000100"
    assert spool.tray_uuid == "BBC7BDD79A66407BB334A9472E3717E6"


@pytest.mark.asyncio
async def test_variance_suppressed_when_scanned_tray_uuid_owned_by_other_spool(db_session):
    """Different-roll guard: when the scanned tray_uuid already belongs to a
    DIFFERENT non-archived spool (a reused tag on a fresh roll), the tolerant
    variance match must NOT hijack — the tray_uuid owner wins and the donor is
    never converged. Protects the reused-tag re-spool sibling guard."""
    donor = Spool(
        material="PETG",
        tag_uid="8C0EF4E700000100",
        tray_uuid="BBC7BDD79A66407BB334A9472E3717E6",
        tag_type="bambulab",
        label_weight=1000,
        core_weight=250,
    )
    other = Spool(
        material="PLA",
        tag_uid="FFEE00112233AABB",
        tray_uuid="C4A25BF1D9054983A9C2E73EE0CF4D5A",
        tag_type="bambulab_reused",
        label_weight=1000,
        core_weight=250,
    )
    for s in (donor, other):
        s.k_profiles = []
        s.assignments = []
        db_session.add(s)
    await db_session.commit()

    # tag_uid first-char varies vs donor, but tray_uuid == other's uuid.
    found = await get_spool_by_tag(db_session, "1C0EF4E700000100", "C4A25BF1D9054983A9C2E73EE0CF4D5A", converge=True)
    # The tray_uuid owner is returned — NOT a variance hijack of the donor.
    assert found is not None and found.id == other.id
    await db_session.commit()
    await db_session.refresh(donor)
    # Donor never converged (its identifiers are untouched).
    assert donor.tag_uid == "8C0EF4E700000100"
    assert donor.tray_uuid == "BBC7BDD79A66407BB334A9472E3717E6"


@pytest.mark.asyncio
async def test_unlink_damping_resolves_scanned_tag_to_assigned_spool(db_session, printer_factory):
    """The auto-unlink damping decision: on an identifier mismatch, resolving the
    scanned tag via get_spool_by_tag returns the SAME spool already assigned to the
    tray (reader variance, not a different roll) → main.py skips the unlink. Models
    printer 3's live DB row exactly. Convergence then makes the mismatch vanish."""
    printer = await printer_factory(model="H2S")
    spool = Spool(
        material="PETG",
        tag_uid="8C0EF4E700000100",
        tray_uuid="BBC7BDD79A66407BB334A9472E3717E6",
        tag_type="bambulab",
        label_weight=1000,
        core_weight=250,
    )
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.commit()
    assignment = SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=0)
    db_session.add(assignment)
    await db_session.commit()

    # The scan the printer keeps sending (tag first-char variance, drifted uuid).
    resolved = await get_spool_by_tag(db_session, "1C0EF4E700000100", "C4A25BF1D9054983A9C2E73EE0CF4D5A", converge=True)
    # Damping predicate holds → main.py keeps the assignment (no unlink/re-assign).
    assert resolved is not None and resolved.id == assignment.spool_id
    await db_session.commit()
    await db_session.refresh(spool)
    # And convergence updated the stored identifiers so the mismatch is gone: the
    # next auto-unlink tick sees spool.tray_uuid == scanned → spool_matches, no flap.
    assert spool.tray_uuid == "C4A25BF1D9054983A9C2E73EE0CF4D5A"


# -- auto_assign_spool (SpoolAssignment creation) ---------------------------


@pytest.mark.asyncio
async def test_auto_assign_creates_assignment(db_session, printer_factory):
    """auto_assign_spool creates a SpoolAssignment for the given slot."""
    from unittest.mock import MagicMock

    printer = await printer_factory()
    spool = await create_spool_from_tray(db_session, SAMPLE_TRAY)
    await db_session.commit()

    mock_pm = MagicMock()
    mock_pm.get_status.return_value = None
    mock_pm.get_client.return_value = None

    assignment = await auto_assign_spool(
        printer_id=printer.id,
        ams_id=0,
        tray_id=2,
        spool=spool,
        printer_manager=mock_pm,
        db=db_session,
    )
    await db_session.commit()

    assert assignment.spool_id == spool.id
    assert assignment.printer_id == printer.id
    assert assignment.ams_id == 0
    assert assignment.tray_id == 2


@pytest.mark.asyncio
async def test_auto_assign_replaces_existing(db_session, printer_factory):
    """auto_assign_spool removes old assignment for the same slot."""
    from unittest.mock import MagicMock

    from sqlalchemy import select

    printer = await printer_factory()

    # Create two spools
    spool1 = Spool(material="PLA", label_weight=1000, core_weight=250)
    spool1.k_profiles = []
    spool1.assignments = []
    db_session.add(spool1)
    await db_session.flush()

    spool2 = Spool(material="PETG", label_weight=1000, core_weight=250)
    spool2.k_profiles = []
    spool2.assignments = []
    db_session.add(spool2)
    await db_session.flush()

    mock_pm = MagicMock()
    mock_pm.get_status.return_value = None
    mock_pm.get_client.return_value = None

    # Assign spool1 to slot
    await auto_assign_spool(printer.id, 0, 0, spool1, mock_pm, db_session)
    await db_session.commit()

    # Assign spool2 to same slot — should replace
    await auto_assign_spool(printer.id, 0, 0, spool2, mock_pm, db_session)
    await db_session.commit()

    result = await db_session.execute(
        select(SpoolAssignment).where(
            SpoolAssignment.printer_id == printer.id,
            SpoolAssignment.ams_id == 0,
            SpoolAssignment.tray_id == 0,
        )
    )
    assignments = result.scalars().all()
    assert len(assignments) == 1
    assert assignments[0].spool_id == spool2.id


@pytest.mark.asyncio
async def test_auto_assign_no_greenlet_error_new_spool(db_session, printer_factory):
    """Creating a SpoolAssignment for a newly created spool must not trigger
    a lazy load on spool.assignments (greenlet_spawn error).

    Regression test for #612: db.add(SpoolAssignment) resolves
    back_populates synchronously. If spool.assignments is uninitialized,
    SQLAlchemy attempts a lazy load outside the async greenlet.
    """
    from unittest.mock import MagicMock

    printer = await printer_factory()
    spool = await create_spool_from_tray(db_session, SAMPLE_TRAY)
    # Don't commit yet — keep spool in same session state as production flow

    mock_pm = MagicMock()
    mock_pm.get_status.return_value = None
    mock_pm.get_client.return_value = None

    # This must NOT raise MissingGreenlet / greenlet_spawn error
    assignment = await auto_assign_spool(
        printer_id=printer.id,
        ams_id=0,
        tray_id=0,
        spool=spool,
        printer_manager=mock_pm,
        db=db_session,
    )
    await db_session.commit()

    assert assignment is not None
    assert assignment.spool_id == spool.id


@pytest.mark.asyncio
async def test_auto_assign_no_greenlet_error_existing_spool(db_session, printer_factory):
    """Creating a SpoolAssignment for an existing spool (from get_spool_by_tag)
    must not trigger a lazy load on spool.assignments.

    Regression test for #612.
    """
    from unittest.mock import MagicMock

    printer = await printer_factory()

    # Create spool directly (simulating one that was created in a previous session)
    spool = Spool(
        material="PLA",
        tray_uuid="AABBCCDD11223344AABBCCDD11223344",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()
    # Expire to clear in-session state — simulates fresh query
    db_session.expire(spool)

    # Look up via get_spool_by_tag (must eagerly load relationships)
    found = await get_spool_by_tag(db_session, "", "AABBCCDD11223344AABBCCDD11223344")
    assert found is not None

    mock_pm = MagicMock()
    mock_pm.get_status.return_value = None
    mock_pm.get_client.return_value = None

    # This must NOT raise MissingGreenlet / greenlet_spawn error
    assignment = await auto_assign_spool(
        printer_id=printer.id,
        ams_id=0,
        tray_id=0,
        spool=found,
        printer_manager=mock_pm,
        db=db_session,
    )
    await db_session.commit()

    assert assignment is not None
    assert assignment.spool_id == found.id


# -- find_matching_untagged_spool -------------------------------------------


@pytest.mark.asyncio
async def test_find_matching_untagged_spool_exact_match(db_session):
    """Finds an untagged spool with matching material, subtype, and color."""
    spool = Spool(
        material="PLA",
        subtype="Basic",
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()

    found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
    assert found is not None
    assert found.id == spool.id


@pytest.mark.asyncio
async def test_find_matching_untagged_spool_skips_tagged(db_session):
    """Spools that already have a tag_uid are not matched."""
    spool = Spool(
        material="PLA",
        subtype="Basic",
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
        tag_uid="1122334455667788",
    )
    db_session.add(spool)
    await db_session.commit()

    found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
    assert found is None


@pytest.mark.asyncio
async def test_find_matching_untagged_spool_skips_uuid_tagged(db_session):
    """Spools that already have a tray_uuid are not matched."""
    spool = Spool(
        material="PLA",
        subtype="Basic",
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
        tray_uuid="AABBCCDD11223344AABBCCDD11223344",
    )
    db_session.add(spool)
    await db_session.commit()

    found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
    assert found is None


@pytest.mark.asyncio
async def test_find_matching_untagged_spool_skips_archived(db_session):
    """Archived spools are not matched."""
    from datetime import datetime

    spool = Spool(
        material="PLA",
        subtype="Basic",
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
        archived_at=datetime.now(),
    )
    db_session.add(spool)
    await db_session.commit()

    found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
    assert found is None


@pytest.mark.asyncio
async def test_find_matching_untagged_spool_wrong_material(db_session):
    """Material mismatch returns None."""
    spool = Spool(
        material="PETG",
        subtype="Basic",
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()

    found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
    assert found is None


@pytest.mark.asyncio
async def test_find_matching_untagged_spool_wrong_color(db_session):
    """Color (rgba) mismatch returns None."""
    spool = Spool(
        material="PLA",
        subtype="Basic",
        rgba="FF0000FF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()

    found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
    assert found is None


@pytest.mark.asyncio
async def test_find_matching_untagged_spool_wrong_subtype(db_session):
    """Subtype mismatch returns None (PLA Matte vs PLA Basic)."""
    spool = Spool(
        material="PLA",
        subtype="Matte",
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()

    found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
    assert found is None


@pytest.mark.asyncio
async def test_find_matching_untagged_spool_fifo(db_session):
    """When multiple match, returns the oldest (FIFO)."""
    import asyncio

    spool_old = Spool(
        material="PLA",
        subtype="Basic",
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool_old)
    await db_session.flush()

    # Small delay to ensure different created_at
    await asyncio.sleep(0.05)

    spool_new = Spool(
        material="PLA",
        subtype="Basic",
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool_new)
    await db_session.commit()

    found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
    assert found is not None
    assert found.id == spool_old.id


@pytest.mark.asyncio
async def test_find_matching_untagged_spool_case_insensitive(db_session):
    """Matching is case-insensitive for material and rgba."""
    spool = Spool(
        material="pla",
        subtype="basic",
        rgba="ffffffff",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()

    found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
    assert found is not None
    assert found.id == spool.id


@pytest.mark.asyncio
async def test_find_matching_untagged_spool_no_subtype(db_session):
    """Tray without subtype matches spool without subtype."""
    tray = {**SAMPLE_TRAY, "tray_sub_brands": "PLA", "tray_type": "PLA"}
    spool = Spool(
        material="PLA",
        subtype=None,
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()

    found = await find_matching_untagged_spool(db_session, tray)
    assert found is not None
    assert found.id == spool.id


@pytest.mark.asyncio
async def test_find_matching_untagged_spool_relationships_loaded(db_session):
    """Matched spool has k_profiles and assignments eagerly loaded."""
    spool = Spool(
        material="PLA",
        subtype="Basic",
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()
    db_session.expire(spool)

    found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
    assert found is not None
    assert _relationship_is_loaded(found, "k_profiles")
    assert _relationship_is_loaded(found, "assignments")


# -- find_matching_untagged_spool: #918 regressions ------------------------


@pytest.mark.asyncio
async def test_find_matching_untagged_spool_null_subtype_fallback(db_session):
    """#918: Quick-Add spool (subtype=NULL) matches when AMS reports a subtype.

    The form's Quick-Add mode only requires `material`, so bulk-logged spools
    have subtype=NULL. Before the fix, the strict `subtype = 'Basic'` filter
    excluded these rows and the system created duplicates on first AMS read.
    """
    spool = Spool(
        material="PLA",
        subtype=None,  # Quick-Add bulk entry
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()

    # Tray reports "PLA Basic" → subtype parsed as "Basic"
    found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
    assert found is not None
    assert found.id == spool.id


@pytest.mark.asyncio
async def test_find_matching_untagged_spool_prefers_exact_subtype_over_null(db_session):
    """#918: When both an exact-subtype and a NULL-subtype row match, exact wins.

    The NULL fallback exists only as a backstop for Quick-Add bulk-logged
    spools — if the user did the work to record subtype="Basic", it must
    take precedence over a vague "PLA" record, even if the latter is older.
    """
    import asyncio

    null_spool = Spool(
        material="PLA",
        subtype=None,  # Older but vague — should NOT win
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(null_spool)
    await db_session.flush()

    await asyncio.sleep(0.05)

    exact_spool = Spool(
        material="PLA",
        subtype="Basic",  # Newer but specific — should win
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(exact_spool)
    await db_session.commit()

    found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
    assert found is not None
    assert found.id == exact_spool.id


@pytest.mark.asyncio
async def test_find_matching_untagged_spool_rejects_non_bambu_brand(db_session):
    """#918: A same-color non-Bambu spool must NOT attract a Bambu UUID.

    Without the brand filter, a Polymaker untagged spool of matching
    material/color would silently acquire a Bambu RFID UUID, leaving the
    user with brand="Polymaker" but a Bambu Lab tray UUID — corrupt data.
    """
    spool = Spool(
        material="PLA",
        subtype="Basic",
        rgba="FFFFFFFF",
        brand="Polymaker",  # NOT Bambu — must be rejected
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()

    found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
    assert found is None


@pytest.mark.asyncio
async def test_find_matching_untagged_spool_accepts_null_brand(db_session):
    """#918: Quick-Add spools with brand=NULL still match a Bambu RFID read.

    Quick-Add doesn't require brand, so a user bulk-logging Bambu spools may
    leave it empty. The matcher allows NULL brand because the alternative
    (forcing every Quick-Add spool to be tagged "Bambu") is the exact
    friction the auto-matcher exists to remove.
    """
    spool = Spool(
        material="PLA",
        subtype="Basic",
        rgba="FFFFFFFF",
        brand=None,  # Quick-Add left brand blank
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()

    found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
    assert found is not None
    assert found.id == spool.id


@pytest.mark.asyncio
async def test_find_matching_untagged_spool_accepts_bambu_brand_variants(db_session):
    """#918: Both 'Bambu' (form dropdown) and 'Bambu Lab' (catalog) match.

    DEFAULT_BRANDS in the form lists 'Bambu'; the catalog uses 'Bambu Lab'.
    Users can pick either. The fuzzy %bambu% LIKE handles both, plus
    'BambuLab', 'bambu lab', etc.
    """
    for brand_value in ("Bambu", "Bambu Lab", "BambuLab", "bambu lab"):
        spool = Spool(
            material="PLA",
            subtype="Basic",
            rgba="FFFFFFFF",
            brand=brand_value,
            label_weight=1000,
            core_weight=250,
        )
        db_session.add(spool)
        await db_session.commit()

        found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
        assert found is not None, f"brand={brand_value!r} should match"
        assert found.id == spool.id

        # Clean up so the next iteration starts fresh.
        await db_session.delete(spool)
        await db_session.commit()


@pytest.mark.asyncio
async def test_find_matching_untagged_spool_null_subtype_with_null_brand(db_session):
    """#918: Pure Quick-Add row (brand=NULL, subtype=NULL) matches.

    This is the exact scenario from Arn0uDz's report: 20 spools logged via
    Quick Add, then placed in the AMS one at a time. Before the fix every
    insertion duplicated; after the fix the first matching row is reused.
    """
    spool = Spool(
        material="PLA",
        subtype=None,
        rgba="FFFFFFFF",
        brand=None,
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()

    found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
    assert found is not None
    assert found.id == spool.id


# -- link_tag_to_inventory_spool -------------------------------------------


@pytest.mark.asyncio
async def test_link_tag_to_inventory_spool(db_session):
    """Links RFID tag data to an existing spool."""
    spool = Spool(
        material="PLA",
        subtype="Basic",
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.flush()

    await link_tag_to_inventory_spool(db_session, spool, SAMPLE_TRAY)
    await db_session.commit()

    assert spool.tag_uid == "AABBCCDD11223344"
    assert spool.tray_uuid == "AABBCCDD11223344AABBCCDD11223344"
    assert spool.data_origin == "rfid_linked"
    assert spool.tag_type == "bambulab"
    assert spool.slicer_filament == "GFL99"


@pytest.mark.asyncio
async def test_link_tag_preserves_existing_slicer_filament(db_session):
    """Does not overwrite an existing slicer_filament preset."""
    spool = Spool(
        material="PLA",
        subtype="Basic",
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
        slicer_filament="CUSTOM01",
        slicer_filament_name="My Custom PLA",
    )
    db_session.add(spool)
    await db_session.flush()

    await link_tag_to_inventory_spool(db_session, spool, SAMPLE_TRAY)
    await db_session.commit()

    assert spool.slicer_filament == "CUSTOM01"
    assert spool.slicer_filament_name == "My Custom PLA"


# -- gradient / multi-color subtype detection --------------------------------


@pytest.mark.asyncio
async def test_create_spool_gradient_from_tray_id_name(db_session):
    """PLA Basic with M* color code → subtype='Gradient'."""
    tray = {
        **SAMPLE_TRAY,
        "tray_sub_brands": "PLA Basic",
        "tray_id_name": "A00-M2",  # Ocean to Meadow
    }
    spool = await create_spool_from_tray(db_session, tray)
    assert spool.material == "PLA"
    assert spool.subtype == "Gradient"


@pytest.mark.asyncio
async def test_create_spool_dual_color_from_tray_id_name(db_session):
    """PLA Silk with A05-M* color code → subtype='Dual Color'."""
    tray = {
        **SAMPLE_TRAY,
        "tray_sub_brands": "PLA Silk",
        "tray_id_name": "A05-M1",  # South Beach
    }
    spool = await create_spool_from_tray(db_session, tray)
    assert spool.material == "PLA"
    assert spool.subtype == "Dual Color"


@pytest.mark.asyncio
async def test_create_spool_tri_color_from_tray_id_name(db_session):
    """PLA Silk with A05-T* color code → subtype='Tri Color'."""
    tray = {
        **SAMPLE_TRAY,
        "tray_sub_brands": "PLA Silk",
        "tray_id_name": "A05-T3",  # Neon City
    }
    spool = await create_spool_from_tray(db_session, tray)
    assert spool.material == "PLA"
    assert spool.subtype == "Tri Color"


@pytest.mark.asyncio
async def test_create_spool_silk_plus_subtype(db_session):
    """PLA Silk+ preserves 'Silk+' subtype (no gradient override)."""
    tray = {
        **SAMPLE_TRAY,
        "tray_sub_brands": "PLA Silk+",
        "tray_id_name": "A06-D0",  # Titan Gray — D code, not M/T
    }
    spool = await create_spool_from_tray(db_session, tray)
    assert spool.material == "PLA"
    assert spool.subtype == "Silk+"


@pytest.mark.asyncio
async def test_create_spool_standard_not_affected(db_session):
    """Standard filaments with D/K/etc codes are not affected."""
    tray = {
        **SAMPLE_TRAY,
        "tray_sub_brands": "PLA Basic",
        "tray_id_name": "A00-D3",  # Dark Gray
    }
    spool = await create_spool_from_tray(db_session, tray)
    assert spool.material == "PLA"
    assert spool.subtype == "Basic"


# -- color resolution (#857) -------------------------------------------------


@pytest.mark.asyncio
async def test_color_resolves_from_catalog_not_suffix_fallback(db_session):
    """Regression for #857 — A17-R1 (PLA Translucent Cherry Pink) must NOT resolve
    to 'Scarlet Red' just because 'R1' also appears in PLA Matte.

    The old resolver fell back to a suffix lookup table when the exact tray_id_name
    wasn't mapped, which produced wrong names across material families. Cross-family
    suffix codes are not globally unique, so only the catalog hex lookup is safe.
    """
    # Seed the catalog with the entry that the Cherry Pink hex should hit.
    db_session.add(
        ColorCatalogEntry(
            manufacturer="Bambu Lab",
            color_name="Cherry Pink",
            hex_color="#F5B6CD",
            material="PLA Translucent",
            is_default=True,
        )
    )
    await db_session.flush()

    tray = {
        **SAMPLE_TRAY,
        "tray_type": "PLA",
        "tray_sub_brands": "PLA Translucent",
        "tray_color": "F5B6CDFF",
        "tray_id_name": "A17-R1",
    }
    spool = await create_spool_from_tray(db_session, tray)
    assert spool.color_name == "Cherry Pink"


@pytest.mark.asyncio
async def test_color_name_is_none_when_catalog_miss_and_code_unreadable(db_session):
    """When the hex isn't in the catalog and tray_id_name is a code ('X##-Y#'),
    color_name must stay None rather than falling through to a wrong suffix match.
    A missing name is preferable to a confidently-wrong one.
    """
    tray = {
        **SAMPLE_TRAY,
        "tray_type": "PLA",
        "tray_sub_brands": "PLA Translucent",
        "tray_color": "F5B6CDFF",  # not seeded
        "tray_id_name": "A17-R1",
    }
    spool = await create_spool_from_tray(db_session, tray)
    assert spool.color_name is None


@pytest.mark.asyncio
async def test_ivory_white_pla_matte_resolves_to_ivory_not_jade(db_session):
    """Regression for #1227 — #FFFFFF is shared by Jade White (PLA Basic),
    Ivory White (PLA Matte), and White (PLA Silk) in the Bambu catalog. The
    matcher must filter by `tray_sub_brands` so a new Ivory White PLA Matte
    roll doesn't auto-name as Jade White just because PLA Basic was inserted
    first.
    """
    # Seed in the order from catalog_defaults.py — PLA Basic first.
    db_session.add(
        ColorCatalogEntry(
            manufacturer="Bambu Lab",
            color_name="Jade White",
            hex_color="#FFFFFF",
            material="PLA Basic",
            is_default=True,
        )
    )
    db_session.add(
        ColorCatalogEntry(
            manufacturer="Bambu Lab",
            color_name="Ivory White",
            hex_color="#FFFFFF",
            material="PLA Matte",
            is_default=True,
        )
    )
    await db_session.flush()

    tray = {
        **SAMPLE_TRAY,
        "tray_type": "PLA",
        "tray_sub_brands": "PLA Matte",
        "tray_color": "FFFFFFFF",
        "tray_id_name": "A01-W1",
    }
    spool = await create_spool_from_tray(db_session, tray)
    assert spool.color_name == "Ivory White", (
        "PLA Matte White must resolve to 'Ivory White', not the PLA Basic 'Jade White' that shares the same hex"
    )


@pytest.mark.asyncio
async def test_pla_silk_white_resolves_to_white_not_jade(db_session):
    """Same shared-hex bug as #1227 but for the third collision: PLA Silk
    White at #FFFFFF must not get the PLA Basic 'Jade White' name either.
    """
    db_session.add(
        ColorCatalogEntry(
            manufacturer="Bambu Lab",
            color_name="Jade White",
            hex_color="#FFFFFF",
            material="PLA Basic",
            is_default=True,
        )
    )
    db_session.add(
        ColorCatalogEntry(
            manufacturer="Bambu Lab",
            color_name="White",
            hex_color="#FFFFFF",
            material="PLA Silk",
            is_default=True,
        )
    )
    await db_session.flush()

    tray = {
        **SAMPLE_TRAY,
        "tray_type": "PLA",
        "tray_sub_brands": "PLA Silk",
        "tray_color": "FFFFFFFF",
        "tray_id_name": "A05-W0",
    }
    spool = await create_spool_from_tray(db_session, tray)
    assert spool.color_name == "White"


@pytest.mark.asyncio
async def test_jade_white_pla_basic_still_resolves_correctly(db_session):
    """Happy-path regression guard for #1227: the PLA Basic Jade White case
    that worked before the fix must still work after it. Catalog has all
    three #FFFFFF entries; the PLA Basic spool must still get 'Jade White'.
    """
    for color_name, material in [
        ("Jade White", "PLA Basic"),
        ("Ivory White", "PLA Matte"),
        ("White", "PLA Silk"),
    ]:
        db_session.add(
            ColorCatalogEntry(
                manufacturer="Bambu Lab",
                color_name=color_name,
                hex_color="#FFFFFF",
                material=material,
                is_default=True,
            )
        )
    await db_session.flush()

    tray = {
        **SAMPLE_TRAY,
        "tray_type": "PLA",
        "tray_sub_brands": "PLA Basic",
        "tray_color": "FFFFFFFF",
        "tray_id_name": "A00-W0",
    }
    spool = await create_spool_from_tray(db_session, tray)
    assert spool.color_name == "Jade White"


@pytest.mark.asyncio
async def test_unknown_material_falls_back_to_hex_only_lookup(db_session):
    """When `tray_sub_brands` is empty (third-party spool / OpenTag tag without
    a Bambu material variant), the material filter is dropped and the lookup
    falls back to hex-only. The deterministic ORDER BY keeps the result
    reproducible across SQLite/PostgreSQL.
    """
    db_session.add(
        ColorCatalogEntry(
            manufacturer="Bambu Lab",
            color_name="Jade White",
            hex_color="#FFFFFF",
            material="PLA Basic",
            is_default=True,
        )
    )
    db_session.add(
        ColorCatalogEntry(
            manufacturer="Bambu Lab",
            color_name="Ivory White",
            hex_color="#FFFFFF",
            material="PLA Matte",
            is_default=True,
        )
    )
    await db_session.flush()

    tray = {
        **SAMPLE_TRAY,
        "tray_type": "PLA",
        "tray_sub_brands": "",  # third-party tag, no material variant
        "tray_color": "FFFFFFFF",
        "tray_id_name": "",
    }
    spool = await create_spool_from_tray(db_session, tray)
    # Either is acceptable so long as the result is deterministic; the first-
    # inserted row (Jade White) wins via ORDER BY id.
    assert spool.color_name == "Jade White"


@pytest.mark.asyncio
async def test_color_name_falls_back_to_readable_tray_id_name(db_session):
    """If tray_id_name is a human-readable label (no code pattern), use it when the
    catalog has no entry for the hex. Preserves behavior for third-party spools whose
    firmware puts a readable string in tray_id_name instead of a Bambu code.
    """
    tray = {
        **SAMPLE_TRAY,
        "tray_color": "123456FF",  # not in catalog
        "tray_id_name": "Custom Purple",  # no '-', readable
    }
    spool = await create_spool_from_tray(db_session, tray)
    assert spool.color_name == "Custom Purple"


@pytest.mark.asyncio
async def test_find_matching_untagged_gradient_spool(db_session):
    """find_matching_untagged_spool matches gradient subtype from tray_id_name."""
    spool = Spool(
        material="PLA",
        subtype="Gradient",
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()

    tray = {
        **SAMPLE_TRAY,
        "tray_sub_brands": "PLA Basic",
        "tray_id_name": "A00-M2",
    }
    found = await find_matching_untagged_spool(db_session, tray)
    assert found is not None
    assert found.id == spool.id


@pytest.mark.asyncio
async def test_find_matching_untagged_gradient_no_match_basic(db_session):
    """A 'Basic' spool does NOT match a Gradient tray (different subtype)."""
    spool = Spool(
        material="PLA",
        subtype="Basic",
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()

    tray = {
        **SAMPLE_TRAY,
        "tray_sub_brands": "PLA Basic",
        "tray_id_name": "A00-M2",  # Gradient
    }
    found = await find_matching_untagged_spool(db_session, tray)
    assert found is None


# -- auto_assign_spool: live cali_idx fallback (P9-3) -------------------------


def _make_state_with_tray(ams_id: int, tray_id: int, cali_idx):
    from unittest.mock import MagicMock

    tray_data = {"id": tray_id, "cali_idx": cali_idx, "tray_color": "FF0000FF", "tray_type": "PLA"}
    ams_data = [{"id": ams_id, "tray": [tray_data]}]
    state = MagicMock()
    state.nozzles = []
    state.raw_data = {"ams": ams_data}
    return state


@pytest.mark.asyncio
async def test_auto_assign_no_kprofile_uses_live_cali_idx(db_session, printer_factory):
    """When no K-profile exists, live tray cali_idx is preserved via extrusion_cali_sel."""
    from unittest.mock import MagicMock

    printer = await printer_factory()
    spool = Spool(material="PLA", label_weight=1000, core_weight=250)
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()

    mqtt_mock = MagicMock()
    state = _make_state_with_tray(ams_id=0, tray_id=1, cali_idx=42)
    mock_pm = MagicMock()
    mock_pm.get_status.return_value = state
    mock_pm.get_client.return_value = mqtt_mock

    await auto_assign_spool(printer.id, 0, 1, spool, mock_pm, db_session)
    await db_session.commit()

    mqtt_mock.extrusion_cali_sel.assert_called_once()
    call_kwargs = mqtt_mock.extrusion_cali_sel.call_args[1]
    assert call_kwargs["cali_idx"] == 42
    assert call_kwargs["ams_id"] == 0
    assert call_kwargs["tray_id"] == 1


@pytest.mark.asyncio
async def test_auto_assign_no_kprofile_no_live_cali_idx_nothing_sent(db_session, printer_factory):
    """When tray has no cali_idx, extrusion_cali_sel is not called."""
    from unittest.mock import MagicMock

    printer = await printer_factory()
    spool = Spool(material="PLA", label_weight=1000, core_weight=250)
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()

    mqtt_mock = MagicMock()
    state = _make_state_with_tray(ams_id=0, tray_id=0, cali_idx=None)
    mock_pm = MagicMock()
    mock_pm.get_status.return_value = state
    mock_pm.get_client.return_value = mqtt_mock

    await auto_assign_spool(printer.id, 0, 0, spool, mock_pm, db_session)
    await db_session.commit()

    mqtt_mock.extrusion_cali_sel.assert_not_called()


@pytest.mark.asyncio
async def test_auto_assign_negative_live_cali_idx_not_sent(db_session, printer_factory):
    """A negative live cali_idx (-1) is invalid and must not be sent."""
    from unittest.mock import MagicMock

    printer = await printer_factory()
    spool = Spool(material="PLA", label_weight=1000, core_weight=250)
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()

    mqtt_mock = MagicMock()
    state = _make_state_with_tray(ams_id=0, tray_id=0, cali_idx=-1)
    mock_pm = MagicMock()
    mock_pm.get_status.return_value = state
    mock_pm.get_client.return_value = mqtt_mock

    await auto_assign_spool(printer.id, 0, 0, spool, mock_pm, db_session)
    await db_session.commit()

    mqtt_mock.extrusion_cali_sel.assert_not_called()


@pytest.mark.asyncio
async def test_auto_assign_kprofile_takes_priority_over_live_cali_idx(db_session, printer_factory):
    """Stored K-profile wins over live tray cali_idx."""
    from unittest.mock import MagicMock, patch

    printer = await printer_factory()

    kp_mock = MagicMock()
    kp_mock.printer_id = printer.id
    kp_mock.nozzle_diameter = "0.4"
    kp_mock.cali_idx = 7
    kp_mock.extruder = None

    # Use a fully-mocked spool so SA relationship instrumentation is bypassed.
    # auto_assign_spool only reads attributes — it never persists via the spool.
    spool = MagicMock(spec=Spool)
    spool.id = 999
    spool.material = "PLA"
    spool.slicer_filament = None
    spool.k_profiles = [kp_mock]
    spool.assignments = []

    mqtt_mock = MagicMock()
    # Live tray has cali_idx=99 — stored profile (7) must win
    state = _make_state_with_tray(ams_id=0, tray_id=0, cali_idx=99)
    mock_pm = MagicMock()
    mock_pm.get_status.return_value = state
    mock_pm.get_client.return_value = mqtt_mock

    await auto_assign_spool(printer.id, 0, 0, spool, mock_pm, db_session)

    mqtt_mock.extrusion_cali_sel.assert_called_once()
    call_kwargs = mqtt_mock.extrusion_cali_sel.call_args[1]
    assert call_kwargs["cali_idx"] == 7  # stored profile, not 99


# -- slot_preset_mappings reconciliation on RFID auto-assign ----------------
#
# The slot card on PrintersPage shows slot_preset_mappings.preset_name first
# in its fallback chain (it's the user-configured override for a slot). When a
# new spool gets auto-assigned via RFID the manual-assign path used to be the
# only one that kept this row in sync, so the slot card kept showing the
# previous spool's preset name until the user opened Configure Slot manually.


@pytest.mark.asyncio
async def test_auto_assign_overwrites_stale_slot_preset_mapping(db_session, printer_factory):
    """Pre-seed a slot_preset_mappings row from a previous spool, run RFID
    auto-assign with a different filament, and verify the row reflects the
    NEW spool's preset (not the stale one). The bug being pinned: the user's
    AMS-B3 (PLA-CF) kept showing 'Bambu PLA Silk+' because the row was last
    written when the PLA Silk+ spool was loaded back in March.
    """
    from unittest.mock import MagicMock

    from sqlalchemy import select as sa_select

    from backend.app.models.slot_preset import SlotPresetMapping

    printer = await printer_factory()
    db_session.add(
        SlotPresetMapping(
            printer_id=printer.id,
            ams_id=1,
            tray_id=2,
            preset_id="GFSA06_09",
            preset_name="Bambu PLA Silk+",
            preset_source="cloud",
        )
    )
    await db_session.commit()

    spool = Spool(
        material="PLA-CF",
        subtype="CF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
        slicer_filament="GFA50",
        slicer_filament_name="Bambu PLA-CF",
        rgba="951E23FF",
    )
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()

    mock_pm = MagicMock()
    mock_pm.get_status.return_value = None
    mock_pm.get_client.return_value = None

    await auto_assign_spool(
        printer_id=printer.id,
        ams_id=1,
        tray_id=2,
        spool=spool,
        printer_manager=mock_pm,
        db=db_session,
        tray_info_idx="GFA50",
    )
    await db_session.commit()

    result = await db_session.execute(
        sa_select(SlotPresetMapping).where(
            SlotPresetMapping.printer_id == printer.id,
            SlotPresetMapping.ams_id == 1,
            SlotPresetMapping.tray_id == 2,
        )
    )
    mapping = result.scalar_one()
    assert mapping.preset_name == "Bambu PLA-CF"
    assert mapping.preset_id == "GFSA50"
    assert mapping.preset_source == "cloud"


@pytest.mark.asyncio
async def test_auto_assign_inserts_slot_preset_when_absent(db_session, printer_factory):
    """No pre-existing row → auto-assign inserts one. Pairs with the upsert
    case to keep both branches of the helper covered from this path."""
    from unittest.mock import MagicMock

    from sqlalchemy import select as sa_select

    from backend.app.models.slot_preset import SlotPresetMapping

    printer = await printer_factory()
    spool = Spool(
        material="PLA-CF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
        slicer_filament="GFA50",
        slicer_filament_name="Bambu PLA-CF",
    )
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()

    mock_pm = MagicMock()
    mock_pm.get_status.return_value = None
    mock_pm.get_client.return_value = None

    await auto_assign_spool(
        printer_id=printer.id,
        ams_id=0,
        tray_id=3,
        spool=spool,
        printer_manager=mock_pm,
        db=db_session,
        tray_info_idx="GFA50",
    )
    await db_session.commit()

    result = await db_session.execute(
        sa_select(SlotPresetMapping).where(
            SlotPresetMapping.printer_id == printer.id,
            SlotPresetMapping.ams_id == 0,
            SlotPresetMapping.tray_id == 3,
        )
    )
    mapping = result.scalar_one()
    assert mapping.preset_id == "GFSA50"
    assert mapping.preset_name == "Bambu PLA-CF"


@pytest.mark.asyncio
async def test_auto_assign_local_preset_uses_local_prefix(db_session, printer_factory):
    """Spools whose slicer_filament is a numeric local-preset id get saved
    with a `local_{n}` preset_id (matches the manual-assign path's shape).
    """
    from unittest.mock import MagicMock

    from sqlalchemy import select as sa_select

    from backend.app.models.slot_preset import SlotPresetMapping

    printer = await printer_factory()
    spool = Spool(
        material="PLA",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
        slicer_filament="50",  # numeric → local preset
        slicer_filament_name="My Custom PLA",
    )
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()

    mock_pm = MagicMock()
    mock_pm.get_status.return_value = None
    mock_pm.get_client.return_value = None

    await auto_assign_spool(
        printer_id=printer.id,
        ams_id=0,
        tray_id=0,
        spool=spool,
        printer_manager=mock_pm,
        db=db_session,
    )
    await db_session.commit()

    result = await db_session.execute(
        sa_select(SlotPresetMapping).where(
            SlotPresetMapping.printer_id == printer.id,
            SlotPresetMapping.ams_id == 0,
            SlotPresetMapping.tray_id == 0,
        )
    )
    mapping = result.scalar_one()
    assert mapping.preset_id == "local_50"
    assert mapping.preset_source == "local"
    assert mapping.preset_name == "My Custom PLA"


# -- attract-exclusions (silent tagless tracking) ---------------------------


@pytest.mark.asyncio
async def test_find_matching_untagged_excludes_assigned_spool(db_session, printer_factory):
    """An untagged spool already bound to an AMS slot must NOT be attracted by a
    new Bambu RFID read on another slot (it is in service elsewhere)."""
    printer = await printer_factory()
    spool = Spool(
        material="PLA",
        subtype="Basic",
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
    )
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()
    db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=0))
    await db_session.commit()

    found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
    assert found is None


@pytest.mark.asyncio
async def test_find_matching_untagged_excludes_ams_auto_origin(db_session):
    """An auto-minted tagless row (data_origin='ams_auto') is the farm's own
    silently-tracked third-party spool — a Bambu tag must never hijack it."""
    spool = Spool(
        material="PLA",
        subtype="Basic",
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
        data_origin="ams_auto",
    )
    db_session.add(spool)
    await db_session.commit()

    found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
    assert found is None


@pytest.mark.asyncio
async def test_find_matching_untagged_still_matches_unassigned_manual(db_session):
    """Regression guard: a normal unassigned manually-logged spool (no origin, no
    assignment) is STILL attracted — the exclusions are narrow."""
    spool = Spool(
        material="PLA",
        subtype="Basic",
        rgba="FFFFFFFF",
        brand="Bambu Lab",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()

    found = await find_matching_untagged_spool(db_session, SAMPLE_TRAY)
    assert found is not None
    assert found.id == spool.id


# -- auto_assign_spool stamps first_loaded_at once --------------------------


@pytest.mark.asyncio
async def test_auto_assign_stamps_first_loaded_once(db_session, printer_factory):
    """auto_assign_spool stamps first_loaded_at on the first assignment and never
    re-stamps (a spool pulled and re-assigned keeps its original in-service time)."""
    import asyncio
    from unittest.mock import MagicMock

    printer = await printer_factory()
    spool = Spool(material="PLA", label_weight=1000, core_weight=250)
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()
    assert spool.first_loaded_at is None

    mock_pm = MagicMock()
    mock_pm.get_status.return_value = None
    mock_pm.get_client.return_value = None

    await auto_assign_spool(printer.id, 0, 0, spool, mock_pm, db_session)
    await db_session.commit()
    stamped = spool.first_loaded_at
    assert stamped is not None

    await asyncio.sleep(0.01)
    # Re-assign the same spool to a different slot → timestamp unchanged.
    await auto_assign_spool(printer.id, 1, 0, spool, mock_pm, db_session)
    await db_session.commit()
    assert spool.first_loaded_at == stamped


# -- K-profile drift re-apply (F3: extracted from main.on_ams_change) --------


class _CaliClient:
    """Client stub recording extrusion_cali_sel publishes."""

    def __init__(self, accept: bool = True):
        self.accept = accept
        self.calls: list[dict] = []

    def extrusion_cali_sel(self, **kw):
        self.calls.append(kw)
        return self.accept


_TAGGED_TRAY = {
    "tag_uid": "AABBCCDD11223344",
    "tray_uuid": "AABBCCDD11223344AABBCCDD11223344",
    "tray_info_idx": "GFL99",
    "cali_idx": -1,  # firmware default — drifted from the stored profile
}


async def _seed_spool_with_kp(db_session, printer_id, *, cali_idx=7, nozzle="0.4", extruder=None):
    from backend.app.models.spool_k_profile import SpoolKProfile

    spool = Spool(material="PLA", slicer_filament="GFL99", label_weight=1000, core_weight=250)
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()
    db_session.add(
        SpoolKProfile(
            spool_id=spool.id,
            printer_id=printer_id,
            nozzle_diameter=nozzle,
            k_value=0.02,
            cali_idx=cali_idx,
            extruder=extruder,
        )
    )
    await db_session.commit()
    return spool


@pytest.fixture
def kdrift_client(monkeypatch):
    """Fresh drift window + a capturing client for the lazily-imported singleton."""
    from backend.app.services import spool_tag_matcher
    from backend.app.services.printer_manager import printer_manager

    spool_tag_matcher._kdrift_window.reset()
    client = _CaliClient()
    monkeypatch.setattr(printer_manager, "get_client", lambda pid: client)
    yield client
    spool_tag_matcher._kdrift_window.reset()


@pytest.mark.asyncio
async def test_kdrift_sends_once_inside_the_window_and_again_after(db_session, printer_factory, kdrift_client):
    """The un-gated version fired one extrusion_cali_sel per AMS push — during an
    identify's tray-state flap that is a write storm into an AMS mid-read. One
    publish per slot per _KDRIFT_RETRY_S; the window elapsing re-arms it."""
    import backend.app.utils.retry_window as rw
    from backend.app.services import spool_tag_matcher
    from backend.app.services.spool_tag_matcher import reapply_k_profile_if_drifted

    printer = await printer_factory()
    spool = await _seed_spool_with_kp(db_session, printer.id)

    assert await reapply_k_profile_if_drifted(db_session, printer.id, 0, 0, _TAGGED_TRAY, spool, None) is True
    assert len(kdrift_client.calls) == 1
    assert kdrift_client.calls[0]["cali_idx"] == 7
    assert kdrift_client.calls[0]["filament_id"] == "GFL99"
    assert kdrift_client.calls[0]["nozzle_diameter"] == "0.4"

    # Same slot, still drifted, next push → suppressed by the window.
    assert await reapply_k_profile_if_drifted(db_session, printer.id, 0, 0, _TAGGED_TRAY, spool, None) is False
    assert len(kdrift_client.calls) == 1

    # Window elapses → re-armed.
    original = rw.monotonic
    rw.monotonic = lambda: original() + spool_tag_matcher._KDRIFT_RETRY_S + 1
    try:
        assert await reapply_k_profile_if_drifted(db_session, printer.id, 0, 0, _TAGGED_TRAY, spool, None) is True
    finally:
        rw.monotonic = original
    assert len(kdrift_client.calls) == 2


@pytest.mark.asyncio
async def test_kdrift_refused_push_is_not_retried_inside_the_window(db_session, printer_factory, monkeypatch):
    """The publish is fire-and-forget BY DESIGN: a refused write (AMS identifying /
    drying) is not inspected and must not re-fire on the very next push — it
    self-heals on a later drift tick once the window elapses."""
    from backend.app.services import spool_tag_matcher
    from backend.app.services.printer_manager import printer_manager
    from backend.app.services.spool_tag_matcher import reapply_k_profile_if_drifted

    spool_tag_matcher._kdrift_window.reset()
    client = _CaliClient(accept=False)  # refused
    monkeypatch.setattr(printer_manager, "get_client", lambda pid: client)
    printer = await printer_factory()
    spool = await _seed_spool_with_kp(db_session, printer.id)

    assert await reapply_k_profile_if_drifted(db_session, printer.id, 0, 0, _TAGGED_TRAY, spool, None) is True
    for _ in range(3):  # three more AMS pushes, still drifted, still refusing
        assert await reapply_k_profile_if_drifted(db_session, printer.id, 0, 0, _TAGGED_TRAY, spool, None) is False
    assert len(client.calls) == 1
    spool_tag_matcher._kdrift_window.reset()


@pytest.mark.asyncio
async def test_kdrift_no_publish_without_drift_or_profile_or_tag(db_session, printer_factory, kdrift_client):
    from backend.app.services.spool_tag_matcher import reapply_k_profile_if_drifted

    printer = await printer_factory()
    spool = await _seed_spool_with_kp(db_session, printer.id)

    # Live cali_idx already equals the stored profile → no publish.
    converged = dict(_TAGGED_TRAY, cali_idx=7)
    assert await reapply_k_profile_if_drifted(db_session, printer.id, 0, 0, converged, spool, None) is False

    # Untagged (tagless) tray → the RFID-identity rule doesn't apply here.
    untagged = dict(_TAGGED_TRAY, tag_uid="0" * 16, tray_uuid="0" * 32, tray_info_idx="")
    assert await reapply_k_profile_if_drifted(db_session, printer.id, 0, 0, untagged, spool, None) is False

    # No spool bound.
    assert await reapply_k_profile_if_drifted(db_session, printer.id, 0, 0, _TAGGED_TRAY, None, None) is False

    # Spool without a stored profile for this printer/nozzle.
    bare = Spool(material="PLA", label_weight=1000, core_weight=250)
    bare.k_profiles = []
    bare.assignments = []
    db_session.add(bare)
    await db_session.commit()
    assert await reapply_k_profile_if_drifted(db_session, printer.id, 0, 0, _TAGGED_TRAY, bare, None) is False

    assert kdrift_client.calls == []


@pytest.mark.asyncio
async def test_kdrift_does_not_lazyload_k_profiles(db_session, printer_factory, kdrift_client):
    """The moved block walked ``spool.k_profiles``; callers hand us spools without
    that relationship loaded, and touching it inside an async session greenlet-crashes
    (the 2026-07-17 bare-tray production failure). The explicit query must be used."""
    from backend.app.services.spool_tag_matcher import reapply_k_profile_if_drifted

    printer = await printer_factory()
    spool = await _seed_spool_with_kp(db_session, printer.id)
    loaded = await db_session.get(Spool, spool.id)
    db_session.expire(loaded, ["k_profiles"])
    assert not _relationship_is_loaded(loaded, "k_profiles")

    assert await reapply_k_profile_if_drifted(db_session, printer.id, 0, 0, _TAGGED_TRAY, loaded, None) is True
    assert len(kdrift_client.calls) == 1


@pytest.mark.asyncio
async def test_kdrift_prefers_the_exact_extruder_profile(db_session, printer_factory, kdrift_client):
    from types import SimpleNamespace

    from backend.app.models.spool_k_profile import SpoolKProfile
    from backend.app.services.spool_tag_matcher import reapply_k_profile_if_drifted

    printer = await printer_factory()
    spool = await _seed_spool_with_kp(db_session, printer.id, cali_idx=3, extruder=0)
    db_session.add(
        SpoolKProfile(
            spool_id=spool.id, printer_id=printer.id, nozzle_diameter="0.4", k_value=0.03, cali_idx=9, extruder=1
        )
    )
    await db_session.commit()

    # ams_extruder_map (string-keyed) puts AMS 0 on extruder 1 → that profile wins.
    state = SimpleNamespace(ams_extruder_map={"0": 1}, nozzles=None)
    assert await reapply_k_profile_if_drifted(db_session, printer.id, 0, 0, _TAGGED_TRAY, spool, state) is True
    assert kdrift_client.calls[0]["cali_idx"] == 9
