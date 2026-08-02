"""Tests for spool_tag_matcher service — RFID auto-assign and relationship loading."""

import logging

import pytest
from sqlalchemy import inspect

from backend.app.models.color_catalog import ColorCatalogEntry
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import spool_binding
from backend.app.services.spool_tag_matcher import (
    auto_assign_spool,
    create_spool_from_tray,
    find_matching_untagged_spool,
    get_spool_by_tag,
    is_bambu_tag,
    is_valid_tag,
    link_tag_to_inventory_spool,
)

_MATCHER_LOGGER = "backend.app.services.spool_tag_matcher"


@pytest.fixture(autouse=True)
def _fresh_windows():
    """Un-armed move damper + cali-sel throttle for every test.

    Both are process-lifetime singletons keyed on DB ids / slot tuples that every
    test's fresh in-memory DB restarts at 1 — so without this, one test's move or
    publish silences the next test's. Reset on both sides.
    """
    from backend.app.services import spool_tag_matcher

    spool_binding._move_damper.reset()
    spool_tag_matcher._cali_sel_window.reset()
    yield
    spool_binding._move_damper.reset()
    spool_tag_matcher._cali_sel_window.reset()


def _matcher_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _MATCHER_LOGGER and r.levelno == logging.WARNING]


def _matcher_infos(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _MATCHER_LOGGER and r.levelno == logging.INFO]


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


# -- variance convergence + the tray_uuid refusal (2026-08-01 re-architecture) --
#
# These pins were REWRITTEN with the resolver-hygiene wave. First-char/short-UID
# tolerance exists for READER quirks on the TAG, and it may never span a ``tray_uuid``
# disagreement: both sides asserting different uuids is positive proof of a different
# roll, so the variance match is refused there and never converged (plan §"Root causes
# confirmed (RFID/binding lane)" — the false-merge hazard). The pre-2026-08-01 shape of
# these tests — variance ACCEPTED across a drifted tray_uuid, then converged onto it —
# is exactly the merge the wave removes.
#
# The refusal is asymmetric ON PURPOSE, and the asymmetry is the correction of
# 2026-08-01: see the sibling-tag section further down. A uuid disagreement falsifies;
# a tag disagreement does not, because one roll carries two tags.


@pytest.mark.asyncio
async def test_variance_match_converges_scanned_identifiers(db_session):
    """A converge=True variance match persists the scanned tag_uid onto the spool, so
    the next read is an exact match — killing the auto-unlink ⇄ re-assign reader-
    variance loop (printer 3 looped all day 2026-07-14). The scan asserts no tray_uuid,
    so there is nothing to disagree about and the reader quirk stands alone."""
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

    found = await get_spool_by_tag(db_session, "1C0EF4E700000100", "", converge=True)
    assert found is not None and found.id == spool.id
    await db_session.commit()
    await db_session.refresh(spool)
    # Converged onto the scanned tag; the uuid the scan never asserted is untouched.
    assert spool.tag_uid == "1C0EF4E700000100"
    assert spool.tray_uuid == "BBC7BDD79A66407BB334A9472E3717E6"

    # The next read is now an EXACT tag match — no variance branch, loop dead.
    again = await get_spool_by_tag(db_session, "1C0EF4E700000100", "")
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

    found = await get_spool_by_tag(db_session, "1C0EF4E700000100", "")
    assert found is not None and found.id == spool.id
    await db_session.commit()
    await db_session.refresh(spool)
    # Untouched.
    assert spool.tag_uid == "8C0EF4E700000100"
    assert spool.tray_uuid == "BBC7BDD79A66407BB334A9472E3717E6"


@pytest.mark.asyncio
async def test_variance_refused_when_scanned_uuid_conflicts_with_the_candidates(db_session, caplog):
    """CROSS-UUID REFUSAL: a first-char tag variance whose scan ALSO asserts a
    different valid tray_uuid is a different roll, not a reader quirk. Refused — and
    never converged, which is what used to make the merge permanent."""
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

    with caplog.at_level(logging.WARNING, logger=_MATCHER_LOGGER):
        found = await get_spool_by_tag(
            db_session, "1C0EF4E700000100", "C4A25BF1D9054983A9C2E73EE0CF4D5A", converge=True
        )

    assert found is None, "both identifiers assert valid, disagreeing values → different roll"
    await db_session.commit()
    await db_session.refresh(spool)
    assert spool.tag_uid == "8C0EF4E700000100", "nothing converged onto the refused candidate"
    assert spool.tray_uuid == "BBC7BDD79A66407BB334A9472E3717E6"
    assert any("Refusing first-char variance match" in m for m in _matcher_warnings(caplog))


@pytest.mark.asyncio
async def test_variance_suppressed_when_scanned_tray_uuid_owned_by_other_spool(db_session, caplog):
    """Different-roll guard: when the scanned tray_uuid already belongs to a DIFFERENT
    non-archived spool, the tolerant variance match must NOT hijack the donor — the
    tray_uuid owner wins and the donor is never converged.

    The uuid owner is reached on the uuid path and returned even though its stored tag
    disagrees with the scan (sibling-tag acceptance). Whether a reused-type row holding
    this uuid is really the roll in hand is the RE-SPOOL guard's question, adjudicated
    in ``spool_respool`` — the resolver's job is only to name the row that owns the
    uuid, and it must never answer with an unrelated row it merely tag-resembles."""
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
    with caplog.at_level(logging.INFO, logger=_MATCHER_LOGGER):
        found = await get_spool_by_tag(
            db_session, "1C0EF4E700000100", "C4A25BF1D9054983A9C2E73EE0CF4D5A", converge=True
        )

    # The tray_uuid owner is returned — NOT a variance hijack of the donor.
    assert found is not None and found.id == other.id
    assert any("[sibling-tag]" in m for m in _matcher_infos(caplog))
    await db_session.commit()
    await db_session.refresh(donor)
    await db_session.refresh(other)
    # Donor never converged (its identifiers are untouched)...
    assert donor.tag_uid == "8C0EF4E700000100"
    assert donor.tray_uuid == "BBC7BDD79A66407BB334A9472E3717E6"
    # ...and neither was the row that WAS returned: the uuid path never converges.
    assert other.tag_uid == "FFEE00112233AABB"


@pytest.mark.asyncio
async def test_unlink_damping_resolves_scanned_tag_to_assigned_spool(db_session, printer_factory):
    """The auto-unlink damping decision: on an identifier mismatch, resolving the
    scanned tag via get_spool_by_tag returns the SAME spool already assigned to the
    tray (reader variance, not a different roll) → main.py skips the unlink. Models
    printer 3's live DB row. Convergence then makes the mismatch vanish.

    REWRITTEN 2026-08-01: the drifted-uuid variant of this scan is now a REFUSAL (a
    uuid disagreement is proof of another roll) — damping through the VARIANCE lane
    survives only for a scan that asserts no conflicting uuid, which is the genuine
    reader-variance shape."""
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

    # The scan the printer keeps sending (tag first-char variance, no uuid asserted).
    resolved = await get_spool_by_tag(db_session, "1C0EF4E700000100", "", converge=True)
    # Damping predicate holds → main.py keeps the assignment (no unlink/re-assign).
    assert resolved is not None and resolved.id == assignment.spool_id
    await db_session.commit()
    await db_session.refresh(spool)
    # And convergence updated the stored tag so the mismatch is gone: the next
    # auto-unlink tick sees spool.tag_uid == scanned → spool_matches, no flap.
    assert spool.tag_uid == "1C0EF4E700000100"


# -- sibling tags: an exact-uuid match with a differing tag IS the same roll --
#
# The 2026-08-01 correction, and the reason the uuid path is asymmetric. A Bambu roll
# carries TWO RFID tags — one per flange — sharing ONE tray_uuid, and the AMS reads
# whichever side faces its antenna (the fork's own ``spool_respool``
# ``RespoolSiblingConflict`` documents the shape). Verified live on production, 4/4
# slots: every slot whose wire tag_uid differed from the bound spool's stored one
# matched EXACTLY on tray_uuid and agreed with the ledger on remaining weight —
# spool 46 (20 % ↔ 180 g), 194 (14 % ↔ 140 g), 196 (100 % ↔ 1000 g), 186 (100 % ↔
# 1000 g). The earlier "chimera" reading of that disagreement (a departed roll's uuid
# merged beside a new roll's tag) is RETRACTED: refusing these matches would have
# minted a duplicate ledger row for each of the four rolls.


@pytest.mark.asyncio
async def test_exact_uuid_match_accepts_a_differing_stored_tag_as_a_sibling_read(db_session, caplog):
    """A uuid match whose stored tag_uid disagrees resolves to that SAME row, and says
    so at INFO. tray_uuid is the roll's identity; tag_uid is a read of one of its two
    chips, so the disagreement is which flange faced the antenna — not which roll."""
    roll = Spool(
        material="PETG",
        tag_uid="EC96F1E700000100",
        tray_uuid="8AC9EC0847FD41D0890870319F2E1975",
        tag_type="bambulab",
        label_weight=1000,
        core_weight=250,
        weight_used=820.0,
    )
    roll.k_profiles = []
    roll.assignments = []
    db_session.add(roll)
    await db_session.commit()

    with caplog.at_level(logging.INFO, logger=_MATCHER_LOGGER):
        found = await get_spool_by_tag(
            db_session, "3CF1F3E700000100", "8AC9EC0847FD41D0890870319F2E1975", converge=True
        )

    assert found is not None and found.id == roll.id, "same tray_uuid → same roll, read on its other tag"
    infos = _matcher_infos(caplog)
    assert any("[sibling-tag]" in m and "second RFID tag of the same roll" in m for m in infos)
    assert not any("Refusing tray_uuid match" in m for m in _matcher_warnings(caplog))

    await db_session.commit()
    await db_session.refresh(roll)
    # NOT converged: which chip faces the antenna is a property of how the roll was
    # seated, not a reader defect to correct — rewriting it would just flip-flop.
    assert roll.tag_uid == "EC96F1E700000100", "the stored tag is left exactly as it was"
    assert roll.tray_uuid == "8AC9EC0847FD41D0890870319F2E1975"
    assert roll.weight_used == 820.0, "and one roll keeps ONE gram history"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spool_id_label", "stored_tag", "wire_tag", "tray_uuid"),
    [
        ("46", "EC96F1E700000100", "3CF1F3E700000100", "8AC9EC0847FD41D0890870319F2E1975"),
        ("194", "A5E7210D00000100", "95F6F50C00000100", "3C78FA47DFCC4F0C8C95566C77A73DCE"),
        ("196", "66839BE000000100", "D6385CEC00000100", "0F8FCF6039964FB68F94A59F8B0897D8"),
        ("186", "CBB0D0FE00000100", "2338393200000100", "A74AC09B2B8443BCB0112C15631EFCEC"),
    ],
)
async def test_every_live_prod_sibling_slot_resolves_to_its_own_row(
    db_session, spool_id_label, stored_tag, wire_tag, tray_uuid
):
    """The four production slots, 2026-08-01. Each would have minted a duplicate row
    under the refusal rule. (Tags abbreviated in the incident note are written out with
    the constant Bambu family suffix "00000100" every full value in that capture
    carries; only the tag DISAGREEMENT and the uuid AGREEMENT are load-bearing.)"""
    roll = Spool(
        material="PETG",
        tag_uid=stored_tag,
        tray_uuid=tray_uuid,
        tag_type="bambulab",
        label_weight=1000,
        core_weight=250,
    )
    roll.k_profiles = []
    roll.assignments = []
    db_session.add(roll)
    await db_session.commit()

    found = await get_spool_by_tag(db_session, wire_tag, tray_uuid, converge=True)
    assert found is not None and found.id == roll.id, f"spool {spool_id_label}"
    await db_session.commit()
    await db_session.refresh(roll)
    assert roll.tag_uid == stored_tag, f"spool {spool_id_label}: stored tag not rewritten"


@pytest.mark.asyncio
async def test_a_uuid_disagreement_still_refuses_on_the_variance_path(db_session, caplog):
    """The asymmetry, pinned from the other side: tolerance on the TAG never spans a
    tray_uuid disagreement, because two rolls cannot share a uuid."""
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

    with caplog.at_level(logging.WARNING, logger=_MATCHER_LOGGER):
        found = await get_spool_by_tag(
            db_session, "1C0EF4E700000100", "C4A25BF1D9054983A9C2E73EE0CF4D5A", converge=True
        )

    assert found is None
    assert any("Refusing first-char variance match" in m for m in _matcher_warnings(caplog))


@pytest.mark.asyncio
async def test_exact_uuid_match_stands_when_the_scan_asserts_no_tag(db_session, caplog):
    """A uuid-only scan (no tag read yet) resolves normally and is not even a sibling
    case — a missing identifier is not a disagreement, so nothing is logged."""
    spool = Spool(
        material="PETG",
        tag_uid="EC9611F900000100",
        tray_uuid="8AC9EC0839D14B2FA7BE9E3A2D5C1F44",
        tag_type="bambulab",
        label_weight=1000,
        core_weight=250,
    )
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.commit()

    with caplog.at_level(logging.INFO, logger=_MATCHER_LOGGER):
        found = await get_spool_by_tag(db_session, "", "8AC9EC0839D14B2FA7BE9E3A2D5C1F44")
    assert found is not None and found.id == spool.id
    assert not any("[sibling-tag]" in m for m in _matcher_infos(caplog))

    zeros = await get_spool_by_tag(db_session, "0000000000000000", "8AC9EC0839D14B2FA7BE9E3A2D5C1F44")
    assert zeros is not None and zeros.id == spool.id, "a zero tag asserts nothing either"


# -- the false-merge pin: the constant Bambu family suffix is NOT identity ----


@pytest.mark.asyncio
async def test_distinct_tags_sharing_the_bambu_family_suffix_never_cross_match(db_session):
    """Every Bambu tag_uid ends "00000100", so the deleted ``%{suffix8}`` LIKE matched
    very nearly the whole inventory — and with converge=True the resulting variance
    match rewrote a second roll's identity onto the first row permanently. Two distinct
    16-char tags that share only that family suffix must resolve to NOTHING."""
    for uid in ("8C0EF4E700000100", "1C63A2B400000100"):
        s = Spool(material="PETG", tag_uid=uid, tag_type="bambulab", label_weight=1000, core_weight=250)
        s.k_profiles = []
        s.assignments = []
        db_session.add(s)
    await db_session.commit()

    assert await get_spool_by_tag(db_session, "3CF1F3E700000100", "", converge=True) is None
    # And no stored row was quietly rewritten on the way out.
    for uid in ("8C0EF4E700000100", "1C63A2B400000100"):
        assert await get_spool_by_tag(db_session, uid, "") is not None


@pytest.mark.asyncio
async def test_short_read_matches_by_leading_bytes(db_session):
    """A 4-byte reader reports the LEADING 8 hex chars of the full uid (the trailing
    ones are the constant family suffix), so a genuine short read matches by PREFIX."""
    spool = Spool(material="PLA", tag_uid="A4501234CCDDEE88", label_weight=1000, core_weight=250)
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.commit()

    found = await get_spool_by_tag(db_session, "A4501234", "")
    assert found is not None and found.id == spool.id


@pytest.mark.asyncio
async def test_short_read_does_not_match_by_trailing_bytes(db_session):
    """The mirror image of the pin above, and the whole point of it: an 8-char value
    that happens to equal the stored tag's TAIL is not that tag."""
    spool = Spool(material="PLA", tag_uid="A4501234CCDDEE88", label_weight=1000, core_weight=250)
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.commit()

    assert await get_spool_by_tag(db_session, "CCDDEE88", "") is None


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


# -- loaded_at re-stampable FIFO ordinal (006-H2S) --------------------------


def _mock_pm():
    from unittest.mock import MagicMock

    pm = MagicMock()
    pm.get_status.return_value = None
    pm.get_client.return_value = None
    return pm


async def _fresh_spool(db_session, **kwargs):
    spool = Spool(material="PLA", label_weight=1000, core_weight=250, **kwargs)
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()
    return spool


@pytest.mark.asyncio
async def test_auto_assign_stamps_loaded_at_on_new_binding(db_session, printer_factory):
    """A first binding stamps loaded_at (the re-stampable FIFO ordinal)."""
    printer = await printer_factory()
    spool = await _fresh_spool(db_session)
    assert spool.loaded_at is None
    await auto_assign_spool(printer.id, 0, 0, spool, _mock_pm(), db_session)
    await db_session.commit()
    assert spool.loaded_at is not None


@pytest.mark.asyncio
async def test_auto_assign_no_restamp_on_same_spool_replay(db_session, printer_factory):
    """Re-detecting the SAME spool on the SAME slot (upsert replay) keeps loaded_at —
    the pairing did not change, so the roll's seating order must not reset."""
    import asyncio

    printer = await printer_factory()
    spool = await _fresh_spool(db_session)
    await auto_assign_spool(printer.id, 0, 0, spool, _mock_pm(), db_session)
    await db_session.commit()
    stamped = spool.loaded_at
    assert stamped is not None

    await asyncio.sleep(0.01)
    await auto_assign_spool(printer.id, 0, 0, spool, _mock_pm(), db_session)  # same spool, same slot
    await db_session.commit()
    assert spool.loaded_at == stamped


@pytest.mark.asyncio
async def test_auto_assign_restamps_on_binding_change(db_session, printer_factory):
    """Binding a DIFFERENT spool into a slot that held another one re-stamps the new
    spool's loaded_at — a binding change is a reliable novelty event."""
    import asyncio

    printer = await printer_factory()
    spool_a = await _fresh_spool(db_session)
    spool_b = await _fresh_spool(db_session)
    await auto_assign_spool(printer.id, 0, 0, spool_a, _mock_pm(), db_session)  # slot holds A
    await auto_assign_spool(printer.id, 0, 1, spool_b, _mock_pm(), db_session)  # B lands elsewhere first
    await db_session.commit()
    b_first = spool_b.loaded_at
    assert b_first is not None

    await asyncio.sleep(0.01)
    await auto_assign_spool(printer.id, 0, 0, spool_b, _mock_pm(), db_session)  # re-bind B onto A's slot
    await db_session.commit()
    assert spool_b.loaded_at is not None and spool_b.loaded_at > b_first


# -- move semantics through the RFID funnel (012-H2S) -----------------------
# The stamp functions themselves moved to services/spool_binding.py; their unit
# tests live in test_spool_binding.py beside them.


@pytest.mark.asyncio
async def test_auto_assign_moves_spool_off_its_previous_slot(db_session, printer_factory):
    """Incident replay (012-H2S): the RFID funnel re-assigning a roll to another tray
    of the same printer must MOVE it — the tray-0 row is gone, exactly one binding
    remains. The pre-fix funnel deleted only the TARGET slot's row, so spool 120 stayed
    bound to tray 0 while also bound to tray 1 and both trays presented one ledger to
    the start gate."""
    from sqlalchemy import select

    printer = await printer_factory()
    spool = await _fresh_spool(db_session)
    await auto_assign_spool(printer.id, 0, 0, spool, _mock_pm(), db_session)
    await db_session.commit()

    await auto_assign_spool(printer.id, 0, 1, spool, _mock_pm(), db_session)
    await db_session.commit()

    result = await db_session.execute(select(SpoolAssignment).where(SpoolAssignment.spool_id == spool.id))
    rows = result.scalars().all()
    assert len(rows) == 1
    assert (rows[0].printer_id, rows[0].ams_id, rows[0].tray_id) == (printer.id, 0, 1)
    all_rows = (await db_session.execute(select(SpoolAssignment))).scalars().all()
    assert len(all_rows) == 1, "the stale tray-0 binding must be gone, not merely superseded"


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


# -- cali-sel throttle on the auto-assign lane (007-H2C bind flap) -----------


def _cali_pm(client):
    """printer_manager stub whose live tray carries a cali_idx and whose client
    records every extrusion_cali_sel publish."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    pm = MagicMock()
    pm.get_status.return_value = SimpleNamespace(
        raw_data={"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PETG", "tray_color": "112233FF"}]}]},
        ams_extruder_map=None,
        nozzles=None,
    )
    pm.get_client.return_value = client
    return pm


async def _spool_with_cali(db_session, printer_id, cali_idx):
    from backend.app.models.spool_k_profile import SpoolKProfile

    spool = Spool(material="PETG", slicer_filament="GFG02", label_weight=1000, core_weight=250)
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()
    db_session.add(
        SpoolKProfile(spool_id=spool.id, printer_id=printer_id, nozzle_diameter="0.4", k_value=0.02, cali_idx=cali_idx)
    )
    await db_session.commit()
    await db_session.refresh(spool, ["k_profiles"])
    return spool


@pytest.mark.asyncio
async def test_cali_sel_identical_republish_is_throttled(db_session, printer_factory):
    """Every bind re-published the slot's calibration selection, so a bind flap was
    also a publish flap into an AMS that was already churning (007-H2C, 51 binds in
    5 m 21 s). A republish of the value the slot already carries is suppressed."""
    printer = await printer_factory()
    spool = await _spool_with_cali(db_session, printer.id, 7)
    client = _CaliClient()

    for _ in range(3):  # same slot, same spool, same cali_idx — wire cadence
        await auto_assign_spool(printer.id, 0, 0, spool, _cali_pm(client), db_session)
        await db_session.commit()

    assert len(client.calls) == 1, "identical republishes inside the window are suppressed"
    assert client.calls[0]["cali_idx"] == 7


@pytest.mark.asyncio
async def test_cali_sel_changed_index_publishes_immediately(db_session, printer_factory):
    """The key carries the cali_idx, so a CHANGED selection forms a NEW key and goes
    out at once — the throttle can never delay a real calibration change."""
    printer = await printer_factory()
    spool = await _spool_with_cali(db_session, printer.id, 7)
    client = _CaliClient()

    await auto_assign_spool(printer.id, 0, 0, spool, _cali_pm(client), db_session)
    await db_session.commit()

    spool.k_profiles[0].cali_idx = 9
    await db_session.commit()
    await auto_assign_spool(printer.id, 0, 0, spool, _cali_pm(client), db_session)
    await db_session.commit()

    assert [c["cali_idx"] for c in client.calls] == [7, 9]


@pytest.mark.asyncio
async def test_cali_sel_window_reopens_after_it_elapses(db_session, printer_factory):
    """A throttle, not a mute: the same selection is re-published once the window
    passes (clock injected through the RetryWindow's module-level monotonic)."""
    import backend.app.utils.retry_window as rw
    from backend.app.services import spool_tag_matcher

    printer = await printer_factory()
    spool = await _spool_with_cali(db_session, printer.id, 7)
    client = _CaliClient()

    await auto_assign_spool(printer.id, 0, 0, spool, _cali_pm(client), db_session)
    await db_session.commit()

    original = rw.monotonic
    rw.monotonic = lambda: original() + spool_tag_matcher._CALI_SEL_RETRY_S + 1
    try:
        await auto_assign_spool(printer.id, 0, 0, spool, _cali_pm(client), db_session)
        await db_session.commit()
    finally:
        rw.monotonic = original

    assert len(client.calls) == 2


# -- auto_assign_spool propagates the writer's damper verdict ----------------


@pytest.mark.asyncio
async def test_auto_assign_returns_none_when_the_move_damper_refuses(db_session, printer_factory):
    """The damper lives in the ONE writer, so the RFID funnel just propagates its
    verdict: None means "no binding change happened" — the DB is untouched and no
    calibration frame was published for a move that never took place."""
    from sqlalchemy import select as sa_select

    printer = await printer_factory()
    spool = await _spool_with_cali(db_session, printer.id, 7)
    client = _CaliClient()

    assert await auto_assign_spool(printer.id, 0, 0, spool, _cali_pm(client), db_session) is not None
    await db_session.commit()
    assert await auto_assign_spool(printer.id, 0, 1, spool, _cali_pm(client), db_session) is not None  # 1st move
    await db_session.commit()

    damped = await auto_assign_spool(printer.id, 0, 0, spool, _cali_pm(client), db_session)  # flip back
    await db_session.commit()

    assert damped is None
    result = await db_session.execute(sa_select(SpoolAssignment).where(SpoolAssignment.spool_id == spool.id))
    rows = list(result.scalars().all())
    assert len(rows) == 1 and (rows[0].ams_id, rows[0].tray_id) == (0, 1), "the binding did not flip back"
    assert len(client.calls) == 2, "and no publish rode the refused bind"
