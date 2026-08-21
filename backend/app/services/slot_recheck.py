"""The operator's "Re-check slot" verb — doctrine rule 12's lane (WS11, incident shape 32).

**What was wrong.** The control existed and could conclude nothing. The endpoint called
``ams_presence.command_identify`` with no ``reason``, so ``_discovery_read_at`` was never
stamped, so ``read_answered_no_tag`` returned False forever; the one decision-table arm that
could act was scoped to spent + TAGGED bindings; and the 400 the operator saw was the farm's
own read holding the 30 s identify gate. On 2026-08-19 that cost 21 minutes of clicking
against total silence before the "New roll…" verb was found.

**Two verbs, two honest jobs** (operator ruling 2026-08-19). The house rule forbids two
implementations of one feature, so the difference has to be real:

* **Re-check slot** — *re-evaluate*. Infers from the slot's physical cycle, and when nothing
  moved it SAYS so and changes nothing. Renamed from "Re-read RFID": on a tagless slot it
  never reads anything, and a label promising a hardware action that cannot happen is how
  the 21 minutes were lost.
* **New roll…** — *assert*. Always mints, needs no cycle
  (``POST /inventory/spools/{id}/new-roll``). Unchanged, and still the escape hatch for
  a swap the farm never observed.

**The contract** (rule 12; the canonical table is ``bambu-ams-behavior``
``resources/spool-subsystem.md`` §4.1, rows R1–R8):

===========================================  ==================================================
Input                                        Verdict
===========================================  ==================================================
no un-acted-on physical cycle                KEEP, and say "nothing moved in this slot"   (R1)
cycle + tag-ness ANSWERED no-tag             MINT a tagless row                     (R2/R3/R5)
cycle + tag FOUND                            the identity lane decides                    (R4)
cycle + tag-ness NOT YET ANSWERABLE           record durable intent, resolve later      (R3/R4)
nothing seated                               refuse, and say why                          (R6)
===========================================  ==================================================

**Conclude on the ANSWER, never on the click.** This is rule 12's load-bearing half. Mid-print
the farm never commands a read (doctrine rule 5), so a slot holding a brand-new or reused-tag
Bambu roll is indistinguishable from a tagless one, and minting tagless there is exactly the
"a guess published into an unresolved slot destroys the firmware's answer" failure
(cross-cutting invariant 5). The honest interim state already exists and is already rendered:
``tray_fields.tray_unread`` — seated but unidentified. **The click adds an OWED READ, not a
conclusion.**

**The cycle window is any un-acted-on cycle, no time limit** (operator ruling). A printer idle
for days still works; there is nothing to expire or tune. Idempotent by construction — a cycle
the farm has already acted on is not one it can act on twice, and the partial unique index
makes a second click on an open intent a no-op.

**THIS MODULE MUST NOT MINT OR BIND.** That is the layering line, and it is the one a
well-meaning implementation crosses: an endpoint that resolves the slot and writes a row
re-creates the legacy inline lane the 2026-08-02 hard cutover deleted ("the slot pipeline is
the sole identity/binding driver"). Instead the click records intent, the answer arrives, the
intent becomes a :class:`~backend.app.services.slot_state.ResolutionContext` input, and
``slot_state.resolve()`` emits the verdict through the same ``_tagless_mint_spec`` path every
other mint uses. One driver, one mint path, one place to test. The acknowledgement's undo
obeys the same rule: it goes through ``spool_tagless.replace_bound_row_with_predecessor``, the
mirror of the module's existing charge-re-attribution lane, never a bespoke write here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.slot_recheck import SlotRecheckIntent
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.schemas.printer import RecheckOutcome
from backend.app.services import ams_presence, spool_tagless
from backend.app.services.printer_manager import printer_manager
from backend.app.services.spool_binding import bound_elsewhere, last_released_from_slot_stmt
from backend.app.services.tray_observation import observe_tray
from backend.app.utils.retry_window import RetryWindow

logger = logging.getLogger(__name__)

Slot = tuple[int, int, int]

#: Binding origin recorded when the acknowledgement's undo restores a displaced row. An
#: operator statement of fact, so it is spelled out rather than reusing a wire-lane origin.
ORIGIN_RECHECK_UNDO = "recheck_undo"

#: How often the open-intent lane may re-command a discovery read on ONE slot.
#:
#: This paces a RETRY, never an identity decision — the same job
#: ``spool_tagless._AUTOCONFIG_RETRY_S`` and ``_RECONCILE_MIN_INTERVAL_S`` do, and expressly
#: not the kind of duration doctrine rule 7 forbids. A successful command self-paces
#: (``ams_presence.identify_in_flight`` holds for ``_IDENTIFY_ACTIVE_S`` and the answer lands
#: inside it); this window only bounds the case where the client REFUSES — drying, an engaged
#: extruder, a disconnect — which stamps nothing and would otherwise re-publish at the ~1 Hz
#: push cadence. Aligned with the identify gate so a refused ask costs at most one publish per
#: gate period.
_ASK_INTERVAL_S = 30.0

_ask_pace = RetryWindow(_ASK_INTERVAL_S)

#: Slots with an OPEN intent. A read-through INDEX of the durable rows, never a second source
#: of truth: it is loaded from the DB once per process and every mutation below writes the row
#: first and the set second. It exists because the pipeline asks "does this slot owe an
#: answer?" for every slot of every ~1 Hz push on every printer, and that question must not
#: cost a query. ``None`` = not loaded yet.
_open_slots: set[Slot] | None = None


def _reset_state() -> None:
    """Drop the process-memory index + ask pacing (tests; mirrors the sibling modules)."""
    global _open_slots, _ask_pace
    _open_slots = None
    _ask_pace = RetryWindow(_ASK_INTERVAL_S)


async def _load_open_slots(db: AsyncSession) -> set[Slot]:
    global _open_slots
    if _open_slots is None:
        rows = (
            await db.execute(
                select(
                    SlotRecheckIntent.printer_id,
                    SlotRecheckIntent.ams_id,
                    SlotRecheckIntent.tray_id,
                ).where(SlotRecheckIntent.resolved_at.is_(None))
            )
        ).all()
        _open_slots = {(int(p), int(a), int(t)) for p, a, t in rows}
        if _open_slots:
            logger.info("[recheck] %d open re-check intent(s) rehydrated from the database", len(_open_slots))
    return _open_slots


async def has_open_intent(db: AsyncSession, printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Does this slot still owe the operator an answer? Cheap after the first call."""
    return (printer_id, ams_id, tray_id) in await _load_open_slots(db)


async def open_intent_slots(db: AsyncSession, slots: list[Slot]) -> set[Slot]:
    """Which of these slots still owe the operator an answer. Bulk form of :func:`has_open_intent`.

    Feeds the assignments listing's ``recheck_pending`` projection (operator decision
    2026-08-20: a re-check the farm cannot conclude yet is a STATE the slot carries, not an
    announcement a toast makes once and loses — the toast is gone the moment the operator
    looks away, and shape 32's whole lesson is that a click with no visible consequence reads
    as a broken control).

    Answered from :data:`_open_slots` — the module's own read-through index — rather than by
    re-querying per listing. That is the same one bulk query :func:`_load_open_slots` already
    runs once per process, and reusing it keeps ONE answer to "does this slot owe an answer?":
    the pipeline decides on that index for every slot of every ~1 Hz push, so a second,
    differently-derived reading of the same rows could only drift from it. It differs from
    :func:`pending_undo_slots` for a reason rather than by accident — the undo offer's
    predicate also involves the CURRENT binding, which no index of the intent rows can hold.
    """
    if not slots:
        return set()
    open_slots = await _load_open_slots(db)
    return {slot for slot in slots if slot in open_slots}


async def get_open(db: AsyncSession, printer_id: int, ams_id: int, tray_id: int) -> SlotRecheckIntent | None:
    """The slot's OPEN intent row, or None."""
    return (
        await db.execute(
            select(SlotRecheckIntent).where(
                SlotRecheckIntent.printer_id == printer_id,
                SlotRecheckIntent.ams_id == ams_id,
                SlotRecheckIntent.tray_id == tray_id,
                SlotRecheckIntent.resolved_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def open_intent(
    db: AsyncSession, printer_id: int, ams_id: int, tray_id: int, *, requested_by: int | None
) -> SlotRecheckIntent:
    """Record the operator's question about this slot. Idempotent per slot.

    A second click while one is still open returns the standing row rather than a second
    question about the same physical event — enforced by the partial unique index, so a race
    between two clients loses the insert instead of creating a duplicate.
    """
    existing = await get_open(db, printer_id, ams_id, tray_id)
    if existing is not None:
        return existing
    intent = SlotRecheckIntent(
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        requested_at=datetime.utcnow(),
        requested_by=requested_by,
    )
    db.add(intent)
    try:
        await db.commit()
    except IntegrityError:
        # The unique index caught a concurrent click. The other one is the question.
        await db.rollback()
        standing = await get_open(db, printer_id, ams_id, tray_id)
        if standing is None:  # pragma: no cover — the index only fires when a row exists
            raise
        return standing
    (await _load_open_slots(db)).add((printer_id, ams_id, tray_id))
    logger.info(
        "[recheck] printer=%d A%dT%d operator re-check recorded (intent %d)", printer_id, ams_id, tray_id, intent.id
    )
    return intent


async def resolve_slot(
    db: AsyncSession, printer_id: int, ams_id: int, tray_id: int, *, minted_spool_id: int | None = None
) -> bool:
    """Close this slot's open intent. Returns True when one was open.

    Called for EVERY conclusion, not only the mint: a tag landing (the identity lane
    decided), a release (the roll left, so there is nothing to conclude) and the re-check's
    own mint all answer the operator's question. ``minted_spool_id`` is set only for the
    click-driven mint, because it is what scopes the acknowledgement.
    """
    intent = await get_open(db, printer_id, ams_id, tray_id)
    if intent is None:
        return False
    intent.resolved_at = datetime.utcnow()
    intent.minted_spool_id = minted_spool_id
    await db.commit()
    (await _load_open_slots(db)).discard((printer_id, ams_id, tray_id))
    logger.info(
        "[recheck] printer=%d A%dT%d intent %d resolved (minted spool=%s)",
        printer_id,
        ams_id,
        tray_id,
        intent.id,
        minted_spool_id if minted_spool_id is not None else "-",
    )
    return True


async def note_operator_statement(db: AsyncSession, printer_id: int, ams_id: int, tray_id: int) -> None:
    """THE hook every operator act that states what is in a slot must call, after its commit.

    Four routes assert a slot's identity by hand — ``POST /inventory/assignments``,
    ``POST /inventory/spools/{id}/new-roll``, ``PATCH /spools/{id}/link-tag`` and
    ``POST /spools/from-slot`` — and each one used to leave the farm's own machinery holding
    a contradicting inference. Two of them, both silent, both ending with the operator's row
    unlinked:

    * an OPEN re-check intent survives the act. The pipeline cannot settle it while the slot
      merely KEEPs (``fingerprint_matches`` is not a settling kind), so the question waits
      through the print, the owed read finally answers no-tag, and row 3½ mints
      ``operator_recheck_mint(replace_existing=True)`` over the row the operator just entered
      by hand;
    * a STALE no-tag answer survives it. Once the manual assign's MQTT config lands and
      ``pre_configured_at`` clears, a TAGGED hand-assigned row meets row 4b′ with an old
      discovery stamp still standing, and ``tagged_swap_no_tag_read`` unlinks it and mints
      tagless in its place.

    Both are the same mistake — an inference outliving the fact that answered it — so both
    close here, in one verb, at the moment the fact becomes durable:

    1. resolve the open intent with ``minted_spool_id=None``. NO undo is offered, and that is
       correct rather than a shortcut: the operator did this deliberately and the farm minted
       nothing, so there is nothing to retract (the acknowledgement is scoped to CLICK-DRIVEN
       mints by construction);
    2. stamp the read economy's identity ledger (``ams_presence.note_operator_statement``), so
       any answer older than the statement stops being one.

    Fully guarded (cross-cutting invariant 10): an inventory route must never 500 because a
    satellite ledger complained. A failure here costs at most the old behaviour.

    Import direction: ``slot_recheck`` already sits above ``ams_presence`` and
    ``spool_tagless``, so the hook lives here and the CONTROLLERS call it. ``spool_binding``
    and ``spool_tagless`` must never call upward into this module.
    """
    try:
        await resolve_slot(db, printer_id, ams_id, tray_id, minted_spool_id=None)
        ams_presence.note_operator_statement(printer_id, ams_id, tray_id)
    except Exception:  # noqa: BLE001 — invariant 10: a route may never fail on this
        logger.exception("[recheck] operator-statement hook failed for printer %s A%sT%s", printer_id, ams_id, tray_id)


def tag_ness_answered(printer_id: int, ams_id: int, tray_id: int, *, seated: bool, identity_asserted: bool) -> bool:
    """Has a commanded discovery read on this slot come back with NO CHIP?

    The single evidence gate between "the operator clicked" and "the farm may mint tagless".

    Delegates to ``ams_presence.read_answered_no_tag`` — the read economy owns the stamps and
    this module must never re-derive them — passing the RFID-PAIR bare-ness the answer lane
    itself already uses (``ams_presence.on_tray_observations`` hands ``close_answered_read``
    exactly ``tray_bare=not obs.identity_asserted`` — no ``tag_uid`` and no ``tray_uuid``,
    the configured ``tray_type``/``tray_info_idx`` carriers of
    ``tray_fields.tray_identity_asserted`` deliberately NOT consulted). That predicate is the
    caller's own constraint by design, which is why row 5a can keep its stricter "neither
    config nor identity" reading without either caller widening the other: row 5a is about a
    BARE tray under a spent tagged binding, this is about whether the firmware found a chip.
    A third-party PETG roll reports ``tray_type: "PETG"`` and ``tag_uid: null`` — configured
    yet chip-less, so bare on THIS reading and not on row 5a's — and it is the commonest
    physical shape on this fleet, so a config-aware reading could never answer for it.
    """
    return ams_presence.read_answered_no_tag(
        printer_id,
        ams_id,
        tray_id,
        tray_seated=seated,
        tray_bare=not identity_asserted,
    )


async def maybe_ask(db: AsyncSession, printer_id: int, ams_id: int, tray_id: int, *, busy: bool, seated: bool) -> bool:
    """Command the discovery read an open intent is waiting on, if now is a moment to ask.

    NOT need-gated, deliberately: the operator's click IS the need (doctrine rule 6's second
    oracle), and ``ams_presence.identify_needed`` would answer None for an ordinary bound
    tagless slot — the exact shape R2/R5 arrive in — leaving the intent waiting on a read
    nothing would ever command. Wire safety is untouched: ``command_identify`` still routes
    through the client, which refuses while drying, identifying, or with filament engaged.

    Three cheap refusals before anything is published, each one a doctrine rule rather than an
    optimisation: nothing seated (invariant 4 — never read a slot with no reason), the printer
    busy (rule 5 — mid-print inserts are never auto-read, and this is exactly when the verdict
    is the honest "queued"), and the answer already in hand (a read that has answered is not a
    reason to read again — invariant 13).

    Returns True when a read was actually issued, so the caller can hang the post-read
    K-profile re-apply off it exactly as the old endpoint did.
    """
    if not seated or busy:
        return False
    if tag_ness_answered(printer_id, ams_id, tray_id, seated=seated, identity_asserted=False):
        return False
    if ams_presence.identify_in_flight(printer_id, ams_id, tray_id):
        return False
    if not _ask_pace.allow((printer_id, ams_id, tray_id)):
        return False
    ok, message = await ams_presence.command_identify(
        printer_id,
        ams_id,
        tray_id,
        source="recheck_intent",
        # ``reason="discovery"`` is the fix at the heart of WS11: the old endpoint passed
        # NONE, so ``command_identify`` never stamped ``_discovery_read_at``, so
        # ``read_answered_no_tag`` was False forever and no lane could ever conclude. The
        # read happened and its answer was thrown away.
        reason="discovery",
        # An explicit operator act bypasses NEED, never wire safety: the client still
        # refuses while drying, identifying, or with filament engaged.
        enforce_need=False,
    )
    if not ok:
        logger.debug("[recheck] printer=%d A%dT%d owed read not issued: %s", printer_id, ams_id, tray_id, message)
    return ok


# --- the verdict: the whole contract in one place ---------------------------


#: How long the re-check may wait for the identity pass to land its conclusion.
#:
#: Only ever entered when the tag-ness answer is ALREADY in hand and the printer is idle —
#: i.e. when the pipeline's next raw push (~1 Hz) will conclude — so this buys the operator
#: the real verdict instead of a "queued" they would have to interpret. It is a RESPONSE
#: budget, not a decision input: on expiry the honest "queued" is returned and the mint
#: still lands a moment later through the acknowledgement.
_RECHECK_SETTLE_BUDGET_S = 3.0
_RECHECK_POLL_S = 0.25


@dataclass(frozen=True)
class RecheckVerdict:
    """What one "Re-check slot" click concluded. The service's answer, whole.

    Every field the endpoint puts on the wire lives here, so the controller maps rather than
    decides: :class:`~backend.app.schemas.printer.SlotRecheckResponse` is built from this
    dataclass field-for-field and adds nothing of its own.

    ``read_issued`` is the honest half the old endpoint threw away. A read the client refuses
    — drying, an identify already in flight, filament engaged, the 30 s ask pace — stamps
    nothing, so the tag-ness stays unanswered and the conclusion is still owed. Reporting it
    is what lets the UI say "asked" only when the farm actually asked; swallowing it is how
    shape 32's operator spent 21 minutes believing a hardware action was happening.
    """

    verdict: RecheckOutcome
    printer_id: int
    ams_id: int
    tray_id: int
    spool_id: int | None = None
    label_weight_g: float | None = None
    brand: str | None = None
    material: str | None = None
    read_issued: bool = False
    undo_available: bool = False


async def evaluate(
    db: AsyncSession, printer_id: int, ams_id: int, tray_id: int, *, requested_by: int | None
) -> RecheckVerdict:
    """Decide what the operator's click concluded — the WHOLE of rule 12's contract table.

    Doctrine rule 12 (operator-ratified 2026-08-19, incident shape 32). Three of the six
    verdict rows used to be decided in the endpoint body, which put half a contract in the
    controller and left the suite reaching for it by monkeypatching the route module. The
    rows are one table and they belong in one place — this one — so the service is the only
    thing a test has to talk to, and a row cannot be changed on one side of the layering
    line without the other noticing.

    ==============================================  ====================================
    Input                                           Verdict
    ==============================================  ====================================
    nothing seated (wire-ASSERTED empty)            ``empty``                        (R6)
    the wire already asserts a tag                  ``identified``       (R4, tag-found)
    no un-acted-on physical cycle                   ``unchanged``                    (R1)
    cycle, and the pipeline concluded in budget     ``minted``                (R2/R5/R8)
    cycle, answer not yet available                 ``queued``                    (R3/R4)
    ==============================================  ====================================

    **This function decides nothing about IDENTITY.** It records intent and reports; the mint
    is emitted by ``slot_state.resolve`` through the same ``_tagless_mint_spec`` path every
    other mint uses (module docstring). Nothing here writes a spool or an assignment — an
    endpoint, or a service standing in for one, that resolved the slot itself would re-create
    the inline lane the 2026-08-02 hard cutover deleted.
    """
    # Read the slot through the SAME observation the pipeline decides on, so this function's
    # preconditions and the table's rows can never disagree about what "seated" or "the wire
    # asserts an identity" means. In particular ``identity_asserted`` here is the RFID pair —
    # ``tray_fields.tray_identity_asserted`` counts a configured ``tray_type`` as identity,
    # which is true for every autoconfigured TAGLESS tray on this fleet and would answer
    # "tag found" for the exact slots this verb exists to serve.
    tray = ams_presence.live_tray(printer_id, ams_id, tray_id)
    obs = observe_tray(printer_id, ams_id, tray) if tray is not None else None
    seated = obs.present if obs is not None else None
    identity = obs.identity_asserted if obs is not None else False

    # R6 — nothing seated. Only a wire-ASSERTED empty refuses; presence UNKNOWN falls
    # through, because an unknown must never be resolved toward "there is nothing there".
    if seated is False:
        return RecheckVerdict(verdict="empty", printer_id=printer_id, ams_id=ams_id, tray_id=tray_id)

    # R4's tag-found half — the RFID lane is the stronger oracle and already owns this slot
    # on every push, so no intent is recorded: there is no question for a human answer to
    # settle. The READ still happens, which is the one thing this branch must not drop —
    # on a tagged slot the old "Re-read RFID" was a genuine and useful hardware action
    # (refresh the remaining-%, re-assert the K-profile), it is guaranteed answerable here,
    # and the rename must not quietly remove a capability. Doctrine rule 12 renamed the
    # verb; it did not narrow it.
    if identity:
        issued, message = await ams_presence.command_identify(
            printer_id, ams_id, tray_id, source="manual_refresh", reason="rfid_refresh", enforce_need=False
        )
        if not issued:
            # Reported, never swallowed: the refusal is the client's wire safety talking and
            # the operator has to be able to see that no read happened.
            logger.info(
                "[recheck] printer=%d A%dT%d tagged-slot refresh not issued: %s", printer_id, ams_id, tray_id, message
            )
        return RecheckVerdict(
            verdict="identified",
            printer_id=printer_id,
            ams_id=ams_id,
            tray_id=tray_id,
            brand=(tray or {}).get("tray_sub_brands") or None,
            material=(tray or {}).get("tray_type") or None,
            read_issued=issued,
        )

    # R1 — rule 12's guard. No un-acted-on physical cycle on this slot means the farm has no
    # evidence anything moved, and the click concludes NOTHING and says so. The escape hatch
    # for a swap the farm never observed is the OTHER verb, "New roll…", which asserts rather
    # than infers and needs no cycle at all.
    if not spool_tagless.qualified_cycle_pending(printer_id, ams_id, tray_id):
        return RecheckVerdict(verdict="unchanged", printer_id=printer_id, ams_id=ams_id, tray_id=tray_id)

    intent = await open_intent(db, printer_id, ams_id, tray_id, requested_by=requested_by)

    # Ask now if this is a moment to ask; ``maybe_ask`` owns every refusal (mid-print, not
    # seated, already answered, in flight, paced) so the policy lives in one place.
    issued = await maybe_ask(
        db,
        printer_id,
        ams_id,
        tray_id,
        busy=ams_presence.printer_running(printer_manager.get_status(printer_id)),
        seated=seated is True,
    )

    minted = await _await_conclusion(db, intent.id)
    if minted is None:
        return RecheckVerdict(
            verdict="queued", printer_id=printer_id, ams_id=ams_id, tray_id=tray_id, read_issued=issued
        )
    return RecheckVerdict(
        verdict="minted",
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        spool_id=minted.id,
        label_weight_g=float(minted.label_weight or 0),
        brand=minted.brand,
        material=minted.material,
        read_issued=issued,
        undo_available=True,
    )


async def _await_conclusion(db: AsyncSession, intent_id: int) -> Spool | None:
    """Wait, briefly and boundedly, for the identity pass to mint — or give up and say so.

    The pipeline runs off the raw MQTT push (~1 Hz), which is the ONLY lane allowed to decide
    identity, so this lane cannot conclude for itself without re-creating the inline lane the
    hard cutover deleted. What it can do is wait out one or two pushes when a conclusion is
    imminent, so R2/R5 get "new roll recorded" rather than a "queued" that resolves a second
    later. Nothing depends on the wait: on expiry the intent is still open and the conclusion
    still lands, announced by the acknowledgement.

    Scoped to THIS CLICK's intent row. The endpoint this replaces watched "the newest
    resolved intent for the slot", which on a slot that had been re-checked before answered
    instantly with YESTERDAY's mint — an offer-bearing ``minted`` verdict naming a roll this
    click did not create, for a question still owed. ``open_intent`` is idempotent per slot,
    so the id is the standing question in both the first-click and the second-click case.
    """
    deadline = asyncio.get_running_loop().time() + _RECHECK_SETTLE_BUDGET_S
    while True:
        await asyncio.sleep(_RECHECK_POLL_S)
        # End this session's read transaction between polls. The conclusion is committed by
        # the pipeline's OWN session (it runs off the MQTT push, which owns no request
        # scope), so a snapshot held open here would poll a view of the database taken
        # before the mint and time out against work that has already landed. Nothing is
        # pending — the intent was committed on the way in — so the rollback only releases
        # the snapshot and expires the identity map.
        await db.rollback()
        minted_id = (
            await db.execute(
                select(SlotRecheckIntent.minted_spool_id).where(
                    SlotRecheckIntent.id == intent_id,
                    SlotRecheckIntent.resolved_at.is_not(None),
                )
            )
        ).scalar_one_or_none()
        if minted_id is not None:
            return await db.get(Spool, minted_id)
        if asyncio.get_running_loop().time() >= deadline:
            return None


# --- the acknowledgement and its undo ---------------------------------------


async def pending_undo(db: AsyncSession, printer_id: int, ams_id: int, tray_id: int) -> SlotRecheckIntent | None:
    """The slot's standing "Restore previous roll" offer, or None.

    Bounded by an EVENT, never a timer (operator instruction 2026-08-19): the offer stands
    while the row the click minted is still the row bound to this slot. The slot's next
    physical cycle, next mint or next bind re-decides the slot and re-binds it, which
    dissolves the offer by cause. There is nothing to expire and no constant to justify.

    Scoped to CLICK-DRIVEN mints by construction — ``minted_spool_id`` is only ever set by the
    re-check conclusion. WS1's automatic long-gap mints raise nothing: roll changes are routine
    on this fleet and the 2026-08-10 wave demoted six non-actionable surfaces to log lines for
    exactly that reason.
    """
    intent = (await newest_minting_intents(db, [(printer_id, ams_id, tray_id)])).get((printer_id, ams_id, tray_id))
    if intent is None:
        return None
    bound = (
        await db.execute(
            select(SpoolAssignment.spool_id).where(
                SpoolAssignment.printer_id == printer_id,
                SpoolAssignment.ams_id == ams_id,
                SpoolAssignment.tray_id == tray_id,
            )
        )
    ).scalar_one_or_none()
    return intent if bound is not None and bound == intent.minted_spool_id else None


async def newest_minting_intents(db: AsyncSession, slots: list[Slot]) -> dict[Slot, SlotRecheckIntent]:
    """The most recent MINTING re-check per slot — the half of the offer predicate that is
    about history rather than about the current binding.

    One builder, two entry points (:func:`pending_undo` for a single slot,
    :func:`pending_undo_slots` for the assignments listing), so "which mint did this slot's
    last re-check create?" is asked in exactly one way.

    "Newest per slot" is resolved by the DATABASE, not by scanning. ``pending_undo_slots``
    runs on every ``GET /inventory/assignments``, and the old shape read EVERY minting intent
    the fleet has ever recorded and discarded all but one per slot in Python — a scan whose
    cost grows with history forever, for an answer that is at most one row per slot. The
    ``max(id)`` group-by subquery is bounded by the SLOT COUNT instead: ``id`` is monotonic,
    so the largest one per (printer, ams, tray) IS the newest mint, and the ``key in wanted``
    filter below still trims the printer-wide prefetch down to the slots asked about.
    """
    if not slots:
        return {}
    wanted = set(slots)
    newest_ids = (
        select(func.max(SlotRecheckIntent.id))
        .where(
            SlotRecheckIntent.minted_spool_id.is_not(None),
            SlotRecheckIntent.printer_id.in_({p for p, _, _ in wanted}),
        )
        .group_by(SlotRecheckIntent.printer_id, SlotRecheckIntent.ams_id, SlotRecheckIntent.tray_id)
    )
    rows = (await db.execute(select(SlotRecheckIntent).where(SlotRecheckIntent.id.in_(newest_ids)))).scalars()
    newest: dict[Slot, SlotRecheckIntent] = {}
    for row in rows:
        key = (row.printer_id, row.ams_id, row.tray_id)
        if key in wanted:
            newest[key] = row
    return newest


async def pending_undo_slots(db: AsyncSession, bound: dict[Slot, int]) -> set[Slot]:
    """Which of these bound slots carry a standing undo offer. Bulk form of :func:`pending_undo`.

    ``bound`` maps each slot to the spool currently bound there, so the caller's own listing
    supplies the binding half of the predicate and this costs ONE query for the whole fleet
    rather than one per slot.
    """
    newest = await newest_minting_intents(db, list(bound))
    return {slot for slot, intent in newest.items() if intent.minted_spool_id == bound.get(slot)}


async def undo(db: AsyncSession, printer_id: int, ams_id: int, tray_id: int) -> tuple[Spool | None, str]:
    """Restore the roll a click-driven mint displaced. Returns ``(restored_row, reason)``.

    The predecessor is adjudicated through ``spool_binding.last_released_from_slot_stmt`` —
    the ONE origin for "which rows last left this slot", shared with the de-bounce donor query
    and the tier-2 spent stamp — taking the NEWEST released row and never scanning past it
    (invariant 11 applied to the query shape: scanning on would walk onto an older residue and
    restore a roll that left this slot weeks ago).

    **Bounded by the click's own instant** (``last_location_at >= intent.requested_at``). The
    only row this undo may hand back is the one the MINT ITSELF displaced, and that row's
    residue is stamped by the mint's own bind sweep — necessarily AFTER the click, since both
    columns are ``datetime.utcnow()``. Without the bound the query happily returns whatever
    left this bay weeks ago and credits it the mistaken row's grams, and it does so most
    readily in the case the offer is most likely to be taken: scenario R5's spent swap, where
    ``slot_pipeline._apply_replace_spent`` HARD-DELETES a pristine drained row through
    ``dispose_provisional_on_tag`` and therefore leaves no residue of its own at all. An empty
    answer is the correct one there — see ``no_predecessor`` below.

    A predecessor that is BOUND ELSEWHERE is declined for the same reason every other reader
    of that residue declines one (``spool_binding.bound_elsewhere``, invariant 11): the roll
    is claimed by a live binding in another tray, and restoring it here would fork one
    physical roll across two slots. It cannot be filtered in SQL — that would silently hand
    back an OLDER residue instead of declining.

    Everything the restore actually writes belongs to
    ``spool_tagless.replace_bound_row_with_predecessor``, the mirror of that module's existing
    charge-re-attribution lane. Nothing is written here except the intent's own outcome.

    ``no_predecessor`` is an honest outcome, not an oversight: the mint's own bind disposes a
    displaced row that was auto-minted AND never fed (``slot_pipeline._dispose_ghost``, under
    ``NEVER_FED_MAX_G``), and a spent-swap retires its drained row outright. In both cases
    what left carried no grams and no operator edits, so there is nothing to hand back — and
    saying so beats resurrecting a husk.
    """
    intent = await pending_undo(db, printer_id, ams_id, tray_id)
    if intent is None:
        return None, "no_offer"
    minted = await db.get(Spool, intent.minted_spool_id)
    if minted is None:
        # Unreachable through the FK in production (ON DELETE SET NULL would have cleared
        # the offer with the row), and kept anyway: the alternative to a named refusal here
        # is an AttributeError inside the restore.
        return None, "mint_gone"

    predecessor = (
        await db.execute(
            last_released_from_slot_stmt(printer_id, ams_id, tray_id)
            .where(Spool.last_location_at >= intent.requested_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if predecessor is None or predecessor.id == minted.id:
        logger.warning(
            "[recheck] printer=%d A%dT%d undo declined: nothing left this slot since the click at %s, "
            "so the mint displaced no row that could be handed back",
            printer_id,
            ams_id,
            tray_id,
            intent.requested_at,
        )
        return None, "no_predecessor"
    if bound_elsewhere(predecessor):
        logger.warning(
            "[recheck] printer=%d A%dT%d undo declined: the displaced row (spool %d) is bound to another "
            "slot now — restoring it here would put one physical roll in two trays (invariant 11)",
            printer_id,
            ams_id,
            tray_id,
            predecessor.id,
        )
        return None, "predecessor_bound_elsewhere"

    assignment = (
        await db.execute(
            select(SpoolAssignment).where(
                SpoolAssignment.printer_id == printer_id,
                SpoolAssignment.ams_id == ams_id,
                SpoolAssignment.tray_id == tray_id,
            )
        )
    ).scalar_one_or_none()
    await spool_tagless.replace_bound_row_with_predecessor(
        db,
        minted,
        predecessor,
        intent.resolved_at or intent.requested_at,
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        fingerprint_color=(assignment.fingerprint_color if assignment is not None else predecessor.rgba),
        fingerprint_type=(assignment.fingerprint_type if assignment is not None else predecessor.material),
        origin=ORIGIN_RECHECK_UNDO,
    )
    # The offer dissolves with the act that consumed it. "Resolved, nothing standing" is
    # ALREADY expressible — ``minted_spool_id IS NULL`` is exactly the shape every
    # non-minting conclusion resolves to (``models/slot_recheck``) — so retracting the
    # outcome needs no ``undone_at`` column and no migration. Without this, ``pending_undo``
    # re-derives its offer from ``bound == minted_spool_id``, and any later event that binds
    # the minted row back to this slot (a release then a de-bounce, say) resurrects a
    # "Restore previous roll" button that can now only 409 forever.
    #
    # The history is not lost with the column: the retraction's own
    # ``[tagless] … retracted`` INFO line and the archived row are the durable record, and
    # they say more than a boolean could.
    intent.minted_spool_id = None
    await db.commit()
    return predecessor, "restored"
