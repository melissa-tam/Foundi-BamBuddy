"""WS11 — "Re-check slot": the operator's click as the farm's second identity oracle.

Doctrine rule 12 (operator-ratified 2026-08-19, incident shape 32). Rule 6 has always named
TWO identity oracles — "an RFID tag **or a human answer**" — and only the tag was ever wired
up; this suite pins the other one. Every case is a row of the contract table
(``bambu-ams-behavior`` ``resources/spool-subsystem.md`` §4.1, "Operator re-check"), named
after it, because a row that cannot be pointed at a passing test is a row that is not true.

=====  ==========================================================================
Row    What it pins
=====  ==========================================================================
R1     No un-acted-on cycle ⇒ KEEP, and the verdict SAYS so (the whole bug was silence)
R2     Roll swapped while idle, click ⇒ MINT
R3     Tagless roll added mid-print, click ⇒ intent, then MINT on the answer
R4     NEW/REUSED RFID roll added mid-print, click ⇒ **never minted tagless**
R5     Click on a spent, tagless, parked slot ⇒ MINT (one-click exit from the park)
R6     Click on an empty slot ⇒ refuse, and say why
R8     The acknowledgement is raised, and its undo restores the prior row
=====  ==========================================================================

Plus the reason the intent is DURABLE at all: click mid-print, restart the process, finish
the print, and the read is still owed and the conclusion still lands.

**R4 is the load-bearing one.** Mid-print the farm never commands a read (rule 5), so a slot
holding a brand-new or reused-tag Bambu roll is indistinguishable from a tagless one, and
minting tagless there would destroy the firmware's pending answer (invariant 5). The click
adds an OWED READ, never a conclusion.
"""

import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.models.slot_recheck import SlotRecheckIntent
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import ams_presence, slot_pipeline, slot_recheck, spool_binding, spool_tagless
from backend.app.services.slot_pipeline import PipelineDeps, run_slot_pipeline
from backend.app.services.slot_state import (
    BindingView,
    DecisionKind,
    ResolutionContext,
    SlotState,
    derive_state,
    resolve,
)
from backend.app.services.tray_observation import observe_tray

PRINTER, AMS, TRAY = 1, 0, 2
SLOT = (PRINTER, AMS, TRAY)

_TAGLESS_DEFAULT = '{"tray_type": "PETG", "tray_color": "000000FF", "tray_info_idx": "GFG02"}'


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state():
    """Every test starts from a cold process — which is also how a restart looks."""
    slot_pipeline._reset_state()
    slot_recheck._reset_state()
    spool_tagless._reset_state()
    ams_presence._reset_state()
    spool_binding._move_damper.reset()
    _FakeClient.last_refresh = None
    yield
    slot_pipeline._reset_state()
    slot_recheck._reset_state()
    spool_tagless._reset_state()
    ams_presence._reset_state()
    spool_binding._move_damper.reset()


class _Recorder:
    def __init__(self, settings: dict[str, str] | None = None):
        self.settings = settings if settings is not None else {}
        self.broadcasts: list[dict] = []
        self.identifies: list[tuple[int, int, int, str]] = []
        self.pushes: list[tuple[int, int, int, int]] = []

    async def get_setting(self, key: str) -> str | None:
        return self.settings.get(key)

    async def broadcast(self, payload: dict) -> None:
        self.broadcasts.append(payload)

    async def schedule_identify(self, printer_id: int, ams_id: int, tray_id: int, reason: str) -> None:
        self.identifies.append((printer_id, ams_id, tray_id, reason))

    async def push_config(self, spool, printer_id: int, ams_id: int, tray_id: int, tray: dict) -> bool:
        self.pushes.append((spool.id, printer_id, ams_id, tray_id))
        return True


class _FakeClient:
    def __init__(self, *, gcode_state: str | None = None):
        self.state = SimpleNamespace(state=gcode_state)

    def ams_unit_drying(self, ams_id: int) -> bool:
        return False

    def ams_write_refusal(self, ams_id: int) -> str | None:
        return None

    #: The last slot a read was commanded on, so a test can assert the read HAPPENED and not
    #: merely that a verdict came back.
    last_refresh: tuple[int, int] | None = None

    def ams_refresh_tray(self, ams_id: int, tray_id: int) -> tuple[bool, str]:
        """The client's own wire-safety refusal — an operator bypass reaches it verbatim.

        Refusing here keeps the route test hermetic AND exercises the honest path: a read
        the hardware will not perform stamps nothing, so the tag-ness stays unanswered and
        the verdict is "queued" rather than a conclusion the farm did not earn.
        """
        _FakeClient.last_refresh = (ams_id, tray_id)
        return False, "Please unload filament first"


@pytest.fixture
def env(monkeypatch):
    recorder = _Recorder({"tagless_default_filament": _TAGLESS_DEFAULT})

    async def fake_get_setting(db, key):
        return recorder.settings.get(key)

    monkeypatch.setattr("backend.app.api.routes.settings.get_setting", fake_get_setting)
    monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: None)
    monkeypatch.setattr(spool_tagless.printer_manager, "get_client", lambda pid: None)
    monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: None)
    return recorder


def _deps(db_session, recorder: _Recorder, *, gcode_state: str | None = None) -> PipelineDeps:
    return PipelineDeps(
        db=db_session,
        client=_FakeClient(gcode_state=gcode_state),
        get_setting=recorder.get_setting,
        schedule_identify=recorder.schedule_identify,
        broadcast=recorder.broadcast,
        push_config=recorder.push_config,
    )


# --- helpers ----------------------------------------------------------------


async def _spool(db_session, **kwargs) -> Spool:
    defaults = {"material": "PETG", "rgba": "000000FF", "label_weight": 1000, "core_weight": 250}
    defaults.update(kwargs)
    spool = Spool(**defaults)
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()
    return spool


async def _bind_row(db_session, spool, **kwargs) -> SpoolAssignment:
    row = SpoolAssignment(
        spool_id=spool.id,
        printer_id=PRINTER,
        ams_id=AMS,
        tray_id=TRAY,
        fingerprint_color=kwargs.pop("fingerprint_color", "000000FF"),
        fingerprint_type=kwargs.pop("fingerprint_type", "PETG"),
        **kwargs,
    )
    db_session.add(row)
    await db_session.commit()
    return row


def _tagless_tray(**over) -> dict:
    tray = {"id": str(TRAY), "state": 10, "tray_type": "PETG", "tray_color": "000000FF", "tray_info_idx": "GFG02"}
    tray.update(over)
    return tray


def _obs(tray: dict | None = None):
    return observe_tray(PRINTER, AMS, tray if tray is not None else _tagless_tray())


def _answer_no_tag(*, age_s: float = 100.0) -> None:
    """Stand in for a commanded discovery read that came back finding NO CHIP.

    Seeds the read economy's own ledger exactly as ``command_identify`` would: the discovery
    stamp is what ``read_answered_no_tag`` reads, ``_slot_read_at`` must NOT be newer (a
    newer value means a tag landed instead), and no echo may be pending (an answer that has
    not settled is not an answer).
    """
    ams_presence._discovery_read_at[SLOT] = time.monotonic() - age_s
    ams_presence._slot_read_at.pop(SLOT, None)
    ams_presence._echo_pending.pop(SLOT, None)


async def _click(db_session) -> SlotRecheckIntent:
    """The operator's click, at the service boundary the route calls."""
    return await slot_recheck.open_intent(db_session, *SLOT, requested_by=None)


async def _push(db_session, env, tray: dict | None = None, *, gcode_state: str | None = None):
    return await run_slot_pipeline(PRINTER, [_obs(tray)], _deps(db_session, env, gcode_state=gcode_state))


async def _bound_spool_id(db_session) -> int | None:
    return (
        await db_session.execute(
            select(SpoolAssignment.spool_id).where(
                SpoolAssignment.printer_id == PRINTER,
                SpoolAssignment.ams_id == AMS,
                SpoolAssignment.tray_id == TRAY,
            )
        )
    ).scalar_one_or_none()


# --- R1: nothing moved ------------------------------------------------------


@pytest.mark.asyncio
async def test_r1_no_cycle_concludes_nothing_and_says_so(db_session, env, monkeypatch):
    """R1 — the click on a slot where nothing moved KEEPs, and the verdict is a SENTENCE.

    The original bug was not a wrong answer, it was silence: the endpoint returned a bare
    200 and the UI had nothing to render. Asserting the no-op alone would re-pass on the
    broken code, so this asserts the VERDICT the operator gets.
    """
    incumbent = await _spool(db_session, weight_used=400.0)
    await _bind_row(db_session, incumbent)
    _answer_no_tag()  # even with the answer in hand, no cycle ⇒ no inference

    verdict = await _recheck(db_session, monkeypatch, seated=True)
    assert verdict.verdict == "unchanged"

    # …and nothing was recorded, so no later push can conclude from a click that was refused.
    assert (await db_session.execute(select(SlotRecheckIntent))).scalars().all() == []
    await _push(db_session, env)
    assert await _bound_spool_id(db_session) == incumbent.id


# --- R2: the recovery path the incident's operator needed -------------------


@pytest.mark.asyncio
async def test_r2_idle_swap_click_mints(db_session, env):
    """R2 — a roll swapped while idle, then clicked: the farm records a NEW roll.

    The 2026-08-19 recovery path. The incumbent row describes filament that no longer
    exists (954 g used of a 1000 g label), which is what staged whole production runs.
    """
    incumbent = await _spool(db_session, weight_used=954.4)
    await _bind_row(db_session, incumbent)
    spool_tagless._pending_physical_cycles.add(SLOT)
    _answer_no_tag()

    await _click(db_session)
    await _push(db_session, env)

    bound = await _bound_spool_id(db_session)
    assert bound is not None and bound != incumbent.id
    fresh = await db_session.get(Spool, bound)
    assert float(fresh.weight_used or 0.0) == 0.0
    assert float(fresh.label_weight) == 1000.0
    # The intent is closed, carrying the row it created — the only thing that scopes the
    # acknowledgement to click-driven mints.
    intent = (await db_session.execute(select(SlotRecheckIntent))).scalars().one()
    assert intent.resolved_at is not None and intent.minted_spool_id == bound


# --- R3: tagless mid-print --------------------------------------------------


@pytest.mark.asyncio
async def test_r3_tagless_mid_print_waits_for_the_answer_then_mints(db_session, env):
    """R3 — added mid-print: the click records intent, and the MINT waits for the answer."""
    incumbent = await _spool(db_session, weight_used=900.0)
    await _bind_row(db_session, incumbent)
    spool_tagless._pending_physical_cycles.add(SLOT)
    await _click(db_session)

    # Mid-print the farm commands no read (doctrine rule 5), so nothing may conclude —
    # even though a STALE discovery stamp from before the print is sitting in the ledger.
    _answer_no_tag()
    await _push(db_session, env, gcode_state="RUNNING")
    assert await _bound_spool_id(db_session) == incumbent.id
    assert await slot_recheck.has_open_intent(db_session, *SLOT) is True

    # The print ends; the answer is now about the roll that is actually there.
    await _push(db_session, env)
    bound = await _bound_spool_id(db_session)
    assert bound is not None and bound != incumbent.id
    assert await slot_recheck.has_open_intent(db_session, *SLOT) is False


# --- R4: the row that forbids click-only minting -----------------------------


@pytest.mark.asyncio
async def test_r4_rfid_roll_added_mid_print_is_never_minted_tagless(db_session, env):
    """R4 — a NEW or REUSED RFID roll inserted mid-print is NOT minted tagless.

    The case rule 12 exists to make safe. The operator clicks; mid-print no read is
    commanded, so the farm cannot know whether the seated roll carries a chip. Minting
    tagless here would publish a guess into an unresolved slot and destroy the firmware's
    own answer (invariant 5) — and would leave a duplicate ledger row for a roll whose tag
    lands seconds later.
    """
    incumbent = await _spool(db_session, weight_used=900.0)
    await _bind_row(db_session, incumbent)
    spool_tagless._pending_physical_cycles.add(SLOT)
    await _click(db_session)

    # Several mid-print pushes, with a stale no-tag stamp standing the whole time.
    _answer_no_tag()
    for _ in range(3):
        await _push(db_session, env, gcode_state="RUNNING")
    assert await _bound_spool_id(db_session) == incumbent.id
    assert (await db_session.execute(select(SlotRecheckIntent))).scalars().one().minted_spool_id is None

    # The print ends and the firmware reads the roll: a REUSED tag on a refilled core.
    # The identity lane decides — not the click — and the intent closes with no tagless mint.
    reused = await _spool(db_session, tag_uid="C93B67FE00000100", tray_uuid="UUID-REUSED", weight_used=0.0)
    await _push(
        db_session,
        env,
        _tagless_tray(tag_uid="C93B67FE00000100", tray_uuid="UUID-REUSED"),
    )
    assert await _bound_spool_id(db_session) == reused.id
    intent = (await db_session.execute(select(SlotRecheckIntent))).scalars().one()
    assert intent.resolved_at is not None
    assert intent.minted_spool_id is None, "a tag-found conclusion must raise no undo offer"


def test_r4_table_refuses_a_tagless_mint_while_the_answer_is_missing():
    """R4, at the table: the re-check input is FALSE until the tag-ness is answered.

    Pinned on the pure function too, because this is the invariant a later session is most
    likely to "simplify" — the click looks like enough evidence, and it is not.
    """
    obs = _obs()
    binding = BindingView(
        spool_id=7,
        is_tagless=True,
        tag_uid=None,
        sibling_tag_uid=None,
        tray_uuid=None,
        spent=False,
        archived=False,
        fingerprint_type="PETG",
        fingerprint_color="000000FF",
        pre_configured=False,
    )
    ctx = ResolutionContext(binding=binding, operator_recheck_answered=False, tagless_default={"tray_type": "PETG"})
    decision = resolve(obs, derive_state(obs, binding), ctx)
    assert decision.kind is DecisionKind.KEEP
    assert decision.reason == "fingerprint_matches"


# --- R5: one-click exit from the spent-latch park ---------------------------


@pytest.mark.asyncio
async def test_r5_click_exits_the_spent_tagless_park(db_session, env):
    """R5 — a spent, tagless, parked slot leaves the park in one click.

    Row 4a otherwise waits for a physical cycle that may never come, and for a tagless
    binding there is no ``spent_swap_no_tag_read`` escape (over a spent TAGLESS row a
    no-tag read proves nothing — same-core ambiguity, rule 11's one-way clause).
    """
    drained = await _spool(db_session, weight_used=1000.0, spent_at=datetime.utcnow(), data_origin="ams_auto")
    await _bind_row(db_session, drained)
    spool_tagless._pending_physical_cycles.add(SLOT)
    _answer_no_tag()

    await _click(db_session)
    await _push(db_session, env)

    # The slot is no longer held by a spent row: a replacement was minted for the roll that
    # is physically there. Identity is asserted on the ROW's state rather than on its id —
    # the pristine drained row is hard-deleted by the canonical disposal and SQLite hands
    # its rowid straight back to the successor.
    bound = await _bound_spool_id(db_session)
    assert bound is not None
    successor = await db_session.get(Spool, bound)
    assert successor.spent_at is None
    assert float(successor.weight_used or 0.0) == 0.0
    intent = (await db_session.execute(select(SlotRecheckIntent))).scalars().one()
    assert intent.resolved_at is not None and intent.minted_spool_id == bound


# --- R6: nothing seated -----------------------------------------------------


@pytest.mark.asyncio
async def test_r6_empty_slot_refuses_and_says_why(db_session, env, monkeypatch):
    """R6 — nothing seated, nothing to conclude. Refused with a renderable verdict."""
    spool_tagless._pending_physical_cycles.add(SLOT)
    verdict = await _recheck(db_session, monkeypatch, seated=False)
    assert verdict.verdict == "empty"
    assert (await db_session.execute(select(SlotRecheckIntent))).scalars().all() == []


# --- R7: per-slot independence ----------------------------------------------


@pytest.mark.asyncio
async def test_r7_each_slot_concludes_independently(db_session):
    """R7 — three slots refilled, each click is its own question."""
    for tray in (0, 1, 3):
        await slot_recheck.open_intent(db_session, PRINTER, AMS, tray, requested_by=None)
    assert await slot_recheck.has_open_intent(db_session, PRINTER, AMS, 0) is True
    assert await slot_recheck.has_open_intent(db_session, PRINTER, AMS, 2) is False

    await slot_recheck.resolve_slot(db_session, PRINTER, AMS, 1, minted_spool_id=None)
    assert await slot_recheck.has_open_intent(db_session, PRINTER, AMS, 1) is False
    assert await slot_recheck.has_open_intent(db_session, PRINTER, AMS, 3) is True


@pytest.mark.asyncio
async def test_a_second_click_is_idempotent(db_session):
    """One open question per slot — enforced by the partial unique index, not by a caller."""
    first = await _click(db_session)
    second = await _click(db_session)
    assert first.id == second.id
    assert len((await db_session.execute(select(SlotRecheckIntent))).scalars().all()) == 1


# --- R8: the acknowledgement and its undo -----------------------------------


@pytest.mark.asyncio
async def test_r8_mint_raises_the_acknowledgement_and_the_undo_restores_the_prior_row(db_session, env):
    """R8 — the same roll re-seated after a jam clear, then clicked: an HONEST false positive.

    It mints (wrongly), says so, and offers a one-click undo that hands the slot back to the
    row that was there — with any grams charged in between. Surfaced rather than hidden.
    """
    incumbent = await _spool(db_session, weight_used=400.0, data_origin="ams_auto")
    await _bind_row(db_session, incumbent)
    spool_tagless._pending_physical_cycles.add(SLOT)
    _answer_no_tag()

    await _click(db_session)
    await _push(db_session, env)
    minted_id = await _bound_spool_id(db_session)
    assert minted_id is not None and minted_id != incumbent.id

    # The acknowledgement stands: this slot's newest re-check minted the row bound here.
    offer = await slot_recheck.pending_undo(db_session, *SLOT)
    assert offer is not None and offer.minted_spool_id == minted_id
    assert await slot_recheck.pending_undo_slots(db_session, {SLOT: minted_id}) == {SLOT}

    # A print charges the mistaken row before the operator notices.
    db_session.add(
        SpoolUsageHistory(spool_id=minted_id, weight_used=120.0, created_at=datetime.utcnow() + timedelta(seconds=5))
    )
    minted = await db_session.get(Spool, minted_id)
    minted.weight_used = 120.0
    await db_session.commit()

    restored, reason = await slot_recheck.undo(db_session, *SLOT)
    assert reason == "restored"
    assert restored.id == incumbent.id
    assert await _bound_spool_id(db_session) == incumbent.id
    # The grams went back with the roll: double entry, not a reset.
    await db_session.refresh(incumbent)
    assert float(incumbent.weight_used) == pytest.approx(520.0)
    assert incumbent.archived_at is None
    moved = (
        await db_session.execute(select(SpoolUsageHistory.spool_id).where(SpoolUsageHistory.weight_used == 120.0))
    ).scalar_one()
    assert moved == incumbent.id
    # …and the offer is gone, by cause: the minted row no longer holds the slot.
    assert await slot_recheck.pending_undo(db_session, *SLOT) is None


@pytest.mark.asyncio
async def test_an_automatic_mint_raises_no_acknowledgement(db_session, env):
    """Only a CLICK-driven mint may offer an undo.

    WS1's automatic long-gap mints must stay quiet: roll changes are routine on this fleet,
    and the 2026-08-10 wave demoted six non-actionable surfaces to log lines for exactly
    that reason. The scoping is structural — ``minted_spool_id`` is only ever written by the
    re-check conclusion — so this pins that the ordinary tagless mint leaves no offer.
    """
    await _push(db_session, env)  # unbound slot, configured tray ⇒ the ordinary tagless mint
    bound = await _bound_spool_id(db_session)
    assert bound is not None
    assert await slot_recheck.pending_undo(db_session, *SLOT) is None
    assert await slot_recheck.pending_undo_slots(db_session, {SLOT: bound}) == set()


@pytest.mark.asyncio
async def test_undo_never_restores_a_roll_that_left_before_the_click(db_session, env):
    """The undo may hand back the row THIS mint displaced — and nothing else.

    R5's swap is where the unbounded query bit hardest: ``_apply_replace_spent`` HARD-DELETES
    a pristine drained row through ``dispose_provisional_on_tag``, so the swap leaves no
    residue of its own. An unbounded "newest row released from this slot" therefore reached
    straight past the event and returned whatever left the bay WEEKS ago — then credited that
    shelf roll the mistaken row's grams and re-bound it into a tray it is not in.

    The bound is the click's own instant. The displaced row's residue is stamped by the
    mint's own bind sweep, so it is necessarily NEWER than ``requested_at`` (both columns are
    ``utcnow()``); anything older belongs to a different physical event. With nothing inside
    the window, ``no_predecessor`` is the honest answer — the same one the ghost-disposal case
    already gives.
    """
    ancient = await _spool(db_session, weight_used=300.0, data_origin="ams_auto")
    spool_binding._stamp_last_location(ancient, printer_id=PRINTER, ams_id=AMS, tray_id=TRAY)
    ancient.last_location_at = datetime.utcnow() - timedelta(hours=1)
    drained = await _spool(db_session, weight_used=1000.0, spent_at=datetime.utcnow(), data_origin="ams_auto")
    await _bind_row(db_session, drained)
    spool_tagless._pending_physical_cycles.add(SLOT)
    _answer_no_tag()

    await _click(db_session)
    await _push(db_session, env)  # R5: REPLACE_SPENT retires the drained row outright

    minted_id = await _bound_spool_id(db_session)
    assert minted_id is not None
    assert await slot_recheck.pending_undo(db_session, *SLOT) is not None, "LIVENESS: the offer stands"

    restored, reason = await slot_recheck.undo(db_session, *SLOT)
    assert reason == "no_predecessor"
    assert restored is None
    await db_session.refresh(ancient)
    assert float(ancient.weight_used) == 300.0, "a roll that left an hour ago is credited nothing"
    assert await _bound_spool_id(db_session) == minted_id, "…and never re-seated into a tray it is not in"


@pytest.mark.asyncio
async def test_undo_declines_a_predecessor_bound_elsewhere(db_session, env):
    """The displaced row was carried to ANOTHER tray before the operator hit undo.

    Restoring it here would put one physical roll in two slots at once — the duplicate-ledger
    error ``spool_assignment.spool_id``'s unique index exists to make impossible (invariant
    11). It cannot be excluded in SQL either: filtering it out would silently hand back an
    OLDER residue instead of declining, which is the same substitution the whole A1 change
    removes. So it is adjudicated and REFUSED, with a reason the UI can render.
    """
    incumbent = await _spool(db_session, weight_used=400.0, data_origin="ams_auto")
    await _bind_row(db_session, incumbent)
    spool_tagless._pending_physical_cycles.add(SLOT)
    _answer_no_tag()

    await _click(db_session)
    await _push(db_session, env)
    minted_id = await _bound_spool_id(db_session)
    assert minted_id is not None and minted_id != incumbent.id

    # The operator physically moves the displaced roll into the neighbouring tray.
    await spool_binding.bind_spool_to_slot(
        db_session,
        incumbent,
        printer_id=PRINTER,
        ams_id=AMS,
        tray_id=TRAY + 1,
        fingerprint_color="000000FF",
        fingerprint_type="PETG",
        origin=spool_binding.OPERATOR_ORIGIN,
    )
    await db_session.commit()

    restored, reason = await slot_recheck.undo(db_session, *SLOT)
    assert reason == "predecessor_bound_elsewhere"
    assert restored is None
    assert await _bound_spool_id(db_session) == minted_id
    still_next_door = (
        await db_session.execute(select(SpoolAssignment.tray_id).where(SpoolAssignment.spool_id == incumbent.id))
    ).scalar_one()
    assert still_next_door == TRAY + 1, "the roll stays exactly where the wire says it is"


@pytest.mark.asyncio
async def test_undo_dissolves_its_own_offer(db_session, env):
    """An act that consumed the offer must retract it — and NULL already says that.

    ``pending_undo`` re-derives the offer from ``bound == minted_spool_id``, so leaving the
    outcome column set after the retraction lets any later event that binds the minted row
    back to this slot (a release, then a de-bounce) resurrect a "Restore previous roll"
    button whose only possible outcome is a 409 forever. Clearing ``minted_spool_id`` is not
    a new state: NULL is exactly what every non-minting conclusion resolves to, which is why
    the retraction needs no ``undone_at`` column and no migration.
    """
    incumbent = await _spool(db_session, weight_used=400.0, data_origin="ams_auto")
    await _bind_row(db_session, incumbent)
    spool_tagless._pending_physical_cycles.add(SLOT)
    _answer_no_tag()

    await _click(db_session)
    await _push(db_session, env)

    restored, reason = await slot_recheck.undo(db_session, *SLOT)
    assert reason == "restored" and restored.id == incumbent.id

    intent = (await db_session.execute(select(SlotRecheckIntent))).scalars().one()
    assert intent.minted_spool_id is None, "the outcome is retracted, not merely superseded"
    assert intent.resolved_at is not None, "…and the intent stays RESOLVED — the question was answered"
    assert await slot_recheck.pending_undo(db_session, *SLOT) is None
    assert await slot_recheck.pending_undo_slots(db_session, {SLOT: incumbent.id}) == set()

    again, reason = await slot_recheck.undo(db_session, *SLOT)
    assert (again, reason) == (None, "no_offer"), "an undo cannot be taken twice"


@pytest.mark.asyncio
async def test_the_offer_lookup_reads_only_the_newest_intent_per_slot(db_session, env):
    """``pending_undo_slots`` runs on EVERY ``GET /inventory/assignments``.

    The old shape read every minting intent the fleet had ever recorded and threw all but one
    per slot away in Python — a scan whose cost grows with history forever, for an answer that
    is at most one row per slot. ``max(id)`` grouped by the slot triple is bounded by the SLOT
    COUNT instead, and ``id`` is monotonic so the largest one per slot IS the newest mint.
    """
    superseded = await _spool(db_session, weight_used=10.0)
    current = await _spool(db_session, weight_used=20.0)
    await db_session.commit()
    for spool in (superseded, current):
        db_session.add(
            SlotRecheckIntent(
                printer_id=PRINTER,
                ams_id=AMS,
                tray_id=TRAY,
                requested_at=datetime.utcnow(),
                resolved_at=datetime.utcnow(),
                minted_spool_id=spool.id,
            )
        )
        await db_session.commit()

    newest = await slot_recheck.newest_minting_intents(db_session, [SLOT])
    assert list(newest) == [SLOT]
    assert newest[SLOT].minted_spool_id == current.id, "an older mint on the same slot is superseded"

    # …and the offer follows the newest intent, not the historical one.
    await _bind_row(db_session, current)
    assert await slot_recheck.pending_undo_slots(db_session, {SLOT: current.id}) == {SLOT}
    assert await slot_recheck.pending_undo_slots(db_session, {SLOT: superseded.id}) == set()


# --- A2: an operator statement closes the question --------------------------


@pytest.mark.asyncio
async def test_an_operator_assign_of_a_tagged_row_is_never_unlinked_by_a_stale_no_tag_answer(db_session, env):
    """The operator hand-assigns a TAGGED roll, and a read from before it is still on file.

    The 2026-08-19 wave widened ``_no_tag_answer_contradicts`` from one quadrant to all four,
    so a stale discovery stamp now reaches row 4b′ — where a TAGGED binding under a
    configured, identity-less tray is ``tagged_swap_no_tag_read``: the operator's row is
    UNLINKED and a tagless row minted over it. (The manual assign's own
    ``pre_configured_at`` shields it only until the MQTT config lands, at which point the
    route clears the stamp and the shield goes with it.)

    A human answer is rule 6's second identity ORACLE, so the hook stamps the read economy
    and the stale answer stops outranking it. The verdict becomes the honest one for silence:
    a tagged row waits for the tag lane.
    """
    tagged = await _spool(db_session, tag_uid="C93B67FE00000100", tray_uuid="U" * 32, weight_used=120.0)
    await db_session.commit()
    _answer_no_tag()  # a discovery read from before the operator touched anything

    await spool_binding.bind_spool_to_slot(
        db_session,
        tagged,
        printer_id=PRINTER,
        ams_id=AMS,
        tray_id=TRAY,
        fingerprint_color="000000FF",
        fingerprint_type="PETG",
        origin=spool_binding.OPERATOR_ORIGIN,
    )
    await db_session.commit()

    # Without the hook the stale answer is live evidence and row 4b′ would act on it.
    assert ams_presence.read_answered_no_tag(*SLOT, tray_seated=True, tray_bare=True) is True

    await slot_recheck.note_operator_statement(db_session, *SLOT)
    assert ams_presence.read_answered_no_tag(*SLOT, tray_seated=True, tray_bare=True) is False

    transitions = await _push(db_session, env)
    assert transitions[0].decision.kind is DecisionKind.KEEP
    assert transitions[0].decision.reason == "tagged_row_awaits_tag_lane"
    assert transitions[0].decision.reason != "tagged_swap_no_tag_read"
    assert await _bound_spool_id(db_session) == tagged.id, "the operator's own row still holds the slot"


@pytest.mark.asyncio
async def test_new_roll_closes_the_open_intent_so_a_later_answer_never_mints_over_it(db_session, env, monkeypatch):
    """Click mid-print, then answer with "New roll…" — the later read must conclude nothing.

    ``resolve_slot`` was reachable from exactly ONE place, the pipeline's own settle step, and
    a slot that merely KEEPs does not settle (``fingerprint_matches`` is not a settling kind).
    So the intent outlived the operator's own answer: the print ended, the owed read came back
    no-tag, and row 3½ minted ``operator_recheck_mint(replace_existing=True)`` straight over
    the row "New roll…" had just created — with the operator's brand, label weight and cost
    on it.
    """
    stale_row = await _spool(db_session, weight_used=954.4, data_origin="ams_auto")
    await _bind_row(db_session, stale_row)
    spool_tagless._pending_physical_cycles.add(SLOT)

    await _click(db_session)
    await _push(db_session, env, gcode_state="RUNNING")  # mid-print: nothing may conclude
    assert await slot_recheck.has_open_intent(db_session, *SLOT) is True

    # The operator answers by hand: "New roll…", with the details only they know.
    #
    # Two pieces of scaffolding, both standing in for live wire the unit suite has no
    # printer for: the slot's live tray, and a ``tagless_default_filament`` in the shape
    # ``_tagless_default`` actually parses (``material``/``rgba`` — the suite-wide fixture
    # uses the tray-field spelling, which that parser reads as "feature off", sending the
    # replacement down its no-default branch where nothing can supply a fingerprint).
    # Doctrine rule 2 is the reason the default branch is the production path here.
    env.settings["tagless_default_filament"] = (
        '{"material": "PETG", "rgba": "000000FF", "tray_info_idx": "GFG02", "brand": "Bambu Lab"}'
    )
    monkeypatch.setattr(spool_tagless, "_live_tray", lambda p, a, t: _tagless_tray())
    fresh = await spool_tagless.apply_fresh_roll(
        db_session, stale_row, PRINTER, AMS, TRAY, brand="Sunlu", label_weight=750
    )
    await db_session.commit()
    await slot_recheck.note_operator_statement(db_session, *SLOT)

    assert await slot_recheck.has_open_intent(db_session, *SLOT) is False
    assert await _bound_spool_id(db_session) == fresh.id

    # The print ends and the owed read finally answers no-tag. It answers about a slot whose
    # question is closed, so nothing re-decides it.
    _answer_no_tag()
    transitions = await _push(db_session, env)

    assert not transitions[0].decision.reason.startswith("operator_recheck")
    assert await _bound_spool_id(db_session) == fresh.id, "the hand-entered row survives"
    survivor = await db_session.get(Spool, fresh.id)
    assert survivor.brand == "Sunlu" and float(survivor.label_weight) == 750.0
    intent = (await db_session.execute(select(SlotRecheckIntent))).scalars().one()
    assert intent.minted_spool_id is None, "the operator's own act offers no undo"


# --- A9.7: the bay emptied, so the question is moot -------------------------


@pytest.mark.asyncio
async def test_a_release_closes_an_open_re_check_intent(db_session, env):
    """The roll left before the owed read could answer — there is nothing left to re-check.

    Leaving the intent open would make the farm go on owing a read for an EMPTY bay, and the
    next insert is a physical cycle the pipeline judges on its own evidence anyway. Resolved
    carrying nothing: no mint happened, so no acknowledgement and no undo may be offered.
    """
    incumbent = await _spool(db_session, weight_used=400.0, data_origin="ams_auto")
    await _bind_row(db_session, incumbent)
    await _click(db_session)
    assert await slot_recheck.has_open_intent(db_session, *SLOT) is True

    # The wire-asserted cleared shape: state 9 with an explicitly empty type.
    transitions = await _push(db_session, env, _tagless_tray(state=9, tray_type="", tray_info_idx=""))

    assert transitions[0].decision.kind is DecisionKind.RELEASE
    assert await slot_recheck.has_open_intent(db_session, *SLOT) is False
    intent = (await db_session.execute(select(SlotRecheckIntent))).scalars().one()
    assert intent.resolved_at is not None
    assert intent.minted_spool_id is None, "a release mints nothing, so it offers no undo"
    assert await slot_recheck.pending_undo(db_session, *SLOT) is None


# --- the reason the intent is durable ---------------------------------------


@pytest.mark.asyncio
async def test_the_intent_survives_a_restart_and_still_concludes(db_session, env):
    """Click mid-print, restart, finish the print — the read is still owed and it lands.

    This is the whole justification for a new durable table (the 2026-08-09 restart-durability
    verdict allows only the incident row, because everything else is a timer or an edge the
    wire re-answers for free). A human's click is neither: no push, no edge and no reconnect
    can restate it, so if it lived in process memory a restart inside the print would silently
    swallow the operator's answer — which is exactly the failure the verb was added to cure.
    """
    incumbent = await _spool(db_session, weight_used=900.0)
    await _bind_row(db_session, incumbent)
    spool_tagless._pending_physical_cycles.add(SLOT)
    await _click(db_session)
    await _push(db_session, env, gcode_state="RUNNING")

    # --- the restart: every in-memory ledger in the AMS stack is emptied ---
    slot_pipeline._reset_state()
    slot_recheck._reset_state()
    spool_tagless._reset_state()
    ams_presence._reset_state()
    assert spool_tagless.qualified_cycle_pending(*SLOT) is False, "the cycle is memory and is gone"

    # The intent is not: it is rehydrated from the database on first use.
    assert await slot_recheck.has_open_intent(db_session, *SLOT) is True

    # The print ends and the owed read finally answers. The conclusion lands with no second
    # click and no surviving cycle — the durable intent carried it across the gap.
    _answer_no_tag()
    await _push(db_session, env)
    bound = await _bound_spool_id(db_session)
    assert bound is not None and bound != incumbent.id
    assert await slot_recheck.has_open_intent(db_session, *SLOT) is False


@pytest.mark.asyncio
async def test_a_de_bounce_never_answers_the_operators_question(db_session, env):
    """A de-bounce may not settle an open re-check — it concludes the OPPOSITE of the click.

    The de-bounce says "the release was spurious, the same roll never left"; the operator has
    just said something moved. It is also assumption-tier evidence (invariant 11) against a
    human answer, which rule 6 names an identity ORACLE. If it settled the intent, the click
    would be silently swallowed and the slot handed back to the very row the operator was
    correcting — so the question stays open and the answered read still concludes.
    """
    departed = await _spool(db_session, weight_used=954.4)
    spool_binding._stamp_last_location(departed, printer_id=PRINTER, ams_id=AMS, tray_id=TRAY)
    await db_session.commit()
    # A measured short absence with no runout cause: the de-bounce lane's own shape.
    ams_presence._reseat[SLOT] = ams_presence._Reseat(30.0, False)

    await _click(db_session)
    await _push(db_session, env)  # answer not in yet ⇒ row 4c de-bounces

    assert await _bound_spool_id(db_session) == departed.id
    assert await slot_recheck.has_open_intent(db_session, *SLOT) is True, (
        "an assumption-tier de-bounce must never close a human's question"
    )

    # The owed read answers; the click now concludes and replaces the de-bounced row.
    _answer_no_tag()
    await _push(db_session, env)
    bound = await _bound_spool_id(db_session)
    assert bound is not None and bound != departed.id
    assert await slot_recheck.has_open_intent(db_session, *SLOT) is False


@pytest.mark.asyncio
async def test_a_pre_configured_binding_is_never_guessed_over(db_session, env):
    """Scenario T13 — operator intent outranks the re-check's inference.

    A pre-assigned slot ("awaiting insert") is itself an operator statement about which roll
    belongs there. Re-checking the slot cannot mean discarding it.
    """
    weighed = await _spool(db_session, weight_used=0.0, label_weight=750)
    await _bind_row(db_session, weighed, pre_configured_at=datetime.utcnow())
    spool_tagless._pending_physical_cycles.add(SLOT)
    _answer_no_tag()

    await _click(db_session)
    await _push(db_session, env)
    assert await _bound_spool_id(db_session) == weighed.id


# --- the verdict: rule 12's whole table at ONE seam --------------------------


async def _recheck(db_session, monkeypatch, *, seated: bool, tray: dict | None = None, client=None):
    """The operator's click, at the one seam that decides it: ``slot_recheck.evaluate``.

    Three of the table's rows (empty / unchanged / identified) used to be decided in the
    endpoint body, which is why this suite once monkeypatched
    ``backend.app.api.routes.printers`` to reach them — a test that has to reach THROUGH a
    controller for business logic is reporting a layering fault, not a testing inconvenience.
    Nothing is stubbed here but the wire itself: the live tray, and the client that answers
    (or refuses) a read.
    """
    live = (
        tray if tray is not None else _tagless_tray(state=10 if seated else 9, **({} if seated else {"tray_type": ""}))
    )
    fake = client if client is not None else _FakeClient()
    monkeypatch.setattr(ams_presence, "live_tray", lambda p, a, t: live)
    monkeypatch.setattr(slot_recheck.printer_manager, "get_client", lambda pid: fake)
    return await slot_recheck.evaluate(db_session, *SLOT, requested_by=None)


class _ReadingClient(_FakeClient):
    """A printer that ACCEPTS the read — the contrast case for ``read_issued``."""

    def ams_refresh_tray(self, ams_id: int, tray_id: int) -> tuple[bool, str]:
        _FakeClient.last_refresh = (ams_id, tray_id)
        return True, "ok"


def _tagged_tray() -> dict:
    """A tray whose RFID pair the wire already asserts — R4's tag-found half."""
    return _tagless_tray(tag_uid="C93B67FE00000100", tray_uuid="UUID-1", tray_sub_brands="Bambu PETG Basic")


@pytest.mark.asyncio
async def test_evaluate_reports_a_tag_the_wire_already_asserts(db_session, env, monkeypatch):
    """R4's tag-found half: the RFID lane is the stronger oracle.

    Nothing for a human answer to add, so no intent is recorded — but the operator still
    gets a sentence naming what the slot holds, instead of the old silence.
    """
    spool_tagless._pending_physical_cycles.add(SLOT)
    verdict = await _recheck(db_session, monkeypatch, seated=True, tray=_tagged_tray(), client=_ReadingClient())
    assert verdict.verdict == "identified"
    assert verdict.brand == "Bambu PETG Basic"
    assert verdict.material == "PETG"
    assert (await db_session.execute(select(SlotRecheckIntent))).scalars().all() == []
    # …and the READ still happens. On a tagged slot the old verb performed a genuine,
    # guaranteed-answerable hardware action (refresh the remaining-%, re-assert the
    # K-profile); the rename must not quietly remove a capability.
    assert _FakeClient.last_refresh == (AMS, TRAY)
    assert verdict.read_issued is True


@pytest.mark.asyncio
async def test_a_refused_read_on_the_tagged_path_is_reported_not_swallowed(db_session, env, monkeypatch):
    """The client refuses the read — and the answer SAYS so, without changing the verdict.

    ``_FakeClient`` refuses exactly as the hardware does with filament engaged. The verdict
    stays ``identified`` — the wire's tag is a fact the read did not establish and does not
    depend on — but ``read_issued`` is False, and that half is the one shape 32 was lost in:
    an operator told a read is happening when the client refused it has no way to learn that
    clicking again cannot help. It is also what the controller gates the K-profile re-apply
    on, and there is no RFID answer coming for that to follow.
    """
    spool_tagless._pending_physical_cycles.add(SLOT)
    verdict = await _recheck(db_session, monkeypatch, seated=True, tray=_tagged_tray())
    assert verdict.verdict == "identified"
    assert verdict.read_issued is False
    assert _FakeClient.last_refresh == (AMS, TRAY), "the slot was ASKED and refused, not silently skipped"


@pytest.mark.asyncio
async def test_evaluate_queues_when_the_answer_is_not_yet_available(db_session, env, monkeypatch):
    """R3/R4 mid-print: intent recorded, and the verdict SAYS it is queued."""
    monkeypatch.setattr(slot_recheck, "_RECHECK_SETTLE_BUDGET_S", 0.05)
    monkeypatch.setattr(slot_recheck, "_RECHECK_POLL_S", 0.01)
    spool_tagless._pending_physical_cycles.add(SLOT)

    verdict = await _recheck(db_session, monkeypatch, seated=True)
    assert verdict.verdict == "queued"
    assert verdict.read_issued is False, "the client refused the discovery read, so nothing was asked"
    intent = (await db_session.execute(select(SlotRecheckIntent))).scalars().one()
    assert intent.resolved_at is None


@pytest.mark.asyncio
async def test_a_new_click_never_reports_an_older_mint_as_its_own(db_session, env, monkeypatch):
    """The settle wait watches THIS click's intent, never the slot's history.

    A slot re-checked before carries a resolved intent naming the row that mint created.
    Watching "the newest resolved intent for this slot" — which is what the endpoint did —
    hands the operator an instant ``minted`` verdict naming YESTERDAY's roll, for a question
    still owed and an answer this click did not earn. While the pipeline has not concluded,
    the honest verdict is ``queued``.
    """
    monkeypatch.setattr(slot_recheck, "_RECHECK_SETTLE_BUDGET_S", 0.05)
    monkeypatch.setattr(slot_recheck, "_RECHECK_POLL_S", 0.01)
    yesterdays_mint = await _spool(db_session)
    await _click(db_session)
    await slot_recheck.resolve_slot(db_session, *SLOT, minted_spool_id=yesterdays_mint.id)

    spool_tagless._pending_physical_cycles.add(SLOT)
    verdict = await _recheck(db_session, monkeypatch, seated=True)
    assert verdict.verdict == "queued"
    assert verdict.spool_id is None


# --- the pending projection: an owed answer is a STATE, not an announcement ---


@pytest.mark.asyncio
async def test_an_open_intent_is_projected_as_recheck_pending(db_session, env):
    """The bulk read behind ``SpoolAssignmentResponse.recheck_pending``.

    Open ⇒ True, resolved ⇒ False, a slot nobody asked about ⇒ False. Operator decision
    2026-08-20: the "queued" verdict rides a toast the operator loses the moment they look
    away, so the slot itself has to carry the fact until it concludes.
    """
    neighbour = (PRINTER, AMS, TRAY + 1)
    await _click(db_session)
    assert await slot_recheck.open_intent_slots(db_session, [SLOT, neighbour]) == {SLOT}
    assert await slot_recheck.open_intent_slots(db_session, []) == set()

    await slot_recheck.resolve_slot(db_session, *SLOT)
    assert await slot_recheck.open_intent_slots(db_session, [SLOT, neighbour]) == set()


@pytest.mark.asyncio
async def test_the_assignments_listing_carries_the_pending_recheck(db_session, env):
    """GET /inventory/assignments projects it per row — the payload the slot card holds."""
    from backend.app.api.routes.inventory import list_assignments

    await _printer_row(db_session)
    spool = await _spool(db_session)
    await _bind_row(db_session, spool)
    await _click(db_session)

    async def _row():
        rows = await list_assignments(printer_id=PRINTER, db=db_session, _=None)
        return next(r for r in rows if r.tray_id == TRAY)

    assert (await _row()).recheck_pending is True
    await slot_recheck.resolve_slot(db_session, *SLOT)
    assert (await _row()).recheck_pending is False


# --- the undo's refusals reach the operator as CODES -------------------------

#: A spool id no row carries. The FK's SET NULL makes ``mint_gone`` unreachable in
#: production; the code still has to be structured, so the test constructs the state the
#: guard exists for directly.
_MISSING_SPOOL_ID = 999_999


async def _printer_row(db_session):
    """The routes' own precondition — a printer that exists."""
    from backend.app.models.printer import Printer

    printer = (await db_session.execute(select(Printer).where(Printer.id == PRINTER))).scalar_one_or_none()
    if printer is None:
        printer = Printer(
            id=PRINTER,
            name="001-H2S",
            serial_number="TESTSERIAL1",
            ip_address="127.0.0.1",
            access_code="00000000",
        )
        db_session.add(printer)
        await db_session.commit()
    return printer


async def _undo_refusal_code(db_session) -> str:
    """POST …/recheck/undo, at the controller, and return the refusal's CODE.

    The reasons are the service's; the 409 SHAPE is the controller's, and it is the half the
    UI can act on — ``ApiError.code`` exists precisely to map a structured detail onto an
    i18n key, and a bare-string detail leaves every non-English operator with an English
    sentence or nothing at all. No business logic is stubbed: each refusal below is produced
    by the real service from a real slot history.
    """
    import backend.app.api.routes.printers as printers_route

    await _printer_row(db_session)
    with pytest.raises(HTTPException) as raised:
        await printers_route.undo_ams_slot_recheck(PRINTER, AMS, TRAY, _=None, db=db_session)
    assert raised.value.status_code == 409
    detail = raised.value.detail
    assert isinstance(detail, dict), "a bare-string detail is exactly what ApiError.code cannot read"
    assert detail["message"], "the English fallback is what a curl/script client gets"
    return detail["code"]


@pytest.mark.asyncio
async def test_the_undo_refuses_an_absent_offer_with_a_code(db_session, env):
    """No click-driven mint holds this slot, so there is nothing to retract."""
    assert await _undo_refusal_code(db_session) == "no_offer"


@pytest.mark.asyncio
async def test_the_undo_refuses_a_vanished_mint_with_a_code(db_session, env):
    """The offer stands but the row it names is gone — refused, never a 500."""
    db_session.add(
        SpoolAssignment(
            spool_id=_MISSING_SPOOL_ID,
            printer_id=PRINTER,
            ams_id=AMS,
            tray_id=TRAY,
            fingerprint_color="000000FF",
            fingerprint_type="PETG",
        )
    )
    await db_session.commit()
    await _click(db_session)
    await slot_recheck.resolve_slot(db_session, *SLOT, minted_spool_id=_MISSING_SPOOL_ID)

    assert await _undo_refusal_code(db_session) == "mint_gone"


@pytest.mark.asyncio
async def test_the_undo_refuses_a_missing_predecessor_with_a_code(db_session, env):
    """R5's spent swap retires its drained row outright, so nothing can be handed back."""
    drained = await _spool(db_session, weight_used=1000.0, spent_at=datetime.utcnow(), data_origin="ams_auto")
    await _bind_row(db_session, drained)
    spool_tagless._pending_physical_cycles.add(SLOT)
    _answer_no_tag()

    await _click(db_session)
    await _push(db_session, env)
    assert await slot_recheck.pending_undo(db_session, *SLOT) is not None, "LIVENESS: the offer stands"

    assert await _undo_refusal_code(db_session) == "no_predecessor"


@pytest.mark.asyncio
async def test_the_undo_refuses_a_predecessor_bound_elsewhere_with_a_code(db_session, env):
    """The displaced roll is in another tray now; restoring it here would fork one roll."""
    incumbent = await _spool(db_session, weight_used=400.0, data_origin="ams_auto")
    await _bind_row(db_session, incumbent)
    spool_tagless._pending_physical_cycles.add(SLOT)
    _answer_no_tag()

    await _click(db_session)
    await _push(db_session, env)
    await spool_binding.bind_spool_to_slot(
        db_session,
        incumbent,
        printer_id=PRINTER,
        ams_id=AMS,
        tray_id=TRAY + 1,
        fingerprint_color="000000FF",
        fingerprint_type="PETG",
        origin=spool_binding.OPERATOR_ORIGIN,
    )
    await db_session.commit()

    assert await _undo_refusal_code(db_session) == "predecessor_bound_elsewhere"
