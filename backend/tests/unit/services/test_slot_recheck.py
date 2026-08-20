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
async def test_r1_no_cycle_concludes_nothing_and_says_so(db_session, env):
    """R1 — the click on a slot where nothing moved KEEPs, and the verdict is a SENTENCE.

    The original bug was not a wrong answer, it was silence: the endpoint returned a bare
    200 and the UI had nothing to render. Asserting the no-op alone would re-pass on the
    broken code, so this asserts the VERDICT the operator gets.
    """
    incumbent = await _spool(db_session, weight_used=400.0)
    await _bind_row(db_session, incumbent)
    _answer_no_tag()  # even with the answer in hand, no cycle ⇒ no inference

    verdict = await _recheck_route(db_session, seated=True)
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
async def test_r6_empty_slot_refuses_and_says_why(db_session, env):
    """R6 — nothing seated, nothing to conclude. Refused with a renderable verdict."""
    spool_tagless._pending_physical_cycles.add(SLOT)
    verdict = await _recheck_route(db_session, seated=False)
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
        SpoolUsageHistory(
            spool_id=minted_id, weight_used=120.0, created_at=datetime.utcnow() + timedelta(seconds=5)
        )
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


# --- the route's verdicts ---------------------------------------------------


async def _recheck_route(db_session, *, seated: bool, tray: dict | None = None):
    """Call the route function directly with a stubbed live wire.

    The route owns three of rule 12's five contract rows (empty / unchanged / identified),
    and those verdicts ARE the fix — the endpoint used to return a bare 200 with nothing
    renderable, which is what the operator experienced as silence.
    """
    import backend.app.api.routes.printers as printers_route
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

    live = tray if tray is not None else _tagless_tray(state=10 if seated else 9, **({} if seated else {"tray_type": ""}))
    original_get_client = printers_route.printer_manager.get_client
    original_live_tray = ams_presence.live_tray
    printers_route.printer_manager.get_client = lambda pid: _FakeClient()
    ams_presence.live_tray = lambda p, a, t: live
    try:
        return await printers_route.recheck_ams_slot(PRINTER, AMS, TRAY, user=None, db=db_session)
    finally:
        printers_route.printer_manager.get_client = original_get_client
        ams_presence.live_tray = original_live_tray


@pytest.mark.asyncio
async def test_the_route_reports_a_tag_the_wire_already_asserts(db_session, env):
    """R4's tag-found half at the route: the RFID lane is the stronger oracle.

    Nothing for a human answer to add, so no intent is recorded — but the operator still
    gets a sentence naming what the slot holds, instead of the old silence.
    """
    spool_tagless._pending_physical_cycles.add(SLOT)
    verdict = await _recheck_route(
        db_session,
        seated=True,
        tray=_tagless_tray(tag_uid="C93B67FE00000100", tray_uuid="UUID-1", tray_sub_brands="Bambu PETG Basic"),
    )
    assert verdict.verdict == "identified"
    assert verdict.brand == "Bambu PETG Basic"
    assert verdict.material == "PETG"
    assert (await db_session.execute(select(SlotRecheckIntent))).scalars().all() == []
    # …and the READ still happens. On a tagged slot the old verb performed a genuine,
    # guaranteed-answerable hardware action (refresh the remaining-%, re-assert the
    # K-profile); the rename must not quietly remove a capability.
    assert _FakeClient.last_refresh == (AMS, TRAY)


@pytest.mark.asyncio
async def test_the_route_queues_when_the_answer_is_not_yet_available(db_session, env, monkeypatch):
    """R3/R4 mid-print at the route: intent recorded, and the verdict SAYS it is queued."""
    monkeypatch.setattr("backend.app.api.routes.printers._RECHECK_SETTLE_BUDGET_S", 0.05)
    monkeypatch.setattr("backend.app.api.routes.printers._RECHECK_POLL_S", 0.01)
    spool_tagless._pending_physical_cycles.add(SLOT)

    verdict = await _recheck_route(db_session, seated=True)
    assert verdict.verdict == "queued"
    intent = (await db_session.execute(select(SlotRecheckIntent))).scalars().one()
    assert intent.resolved_at is None
