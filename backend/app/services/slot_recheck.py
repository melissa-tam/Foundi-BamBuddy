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
  (``POST /inventory/spools/{id}/tagless-fresh``). Unchanged, and still the escape hatch for
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

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.slot_recheck import SlotRecheckIntent
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import ams_presence, spool_tagless
from backend.app.services.spool_binding import last_released_from_slot_stmt
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
            logger.info(
                "[recheck] %d open re-check intent(s) rehydrated from the database", len(_open_slots)
            )
    return _open_slots


async def has_open_intent(db: AsyncSession, printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Does this slot still owe the operator an answer? Cheap after the first call."""
    return (printer_id, ams_id, tray_id) in await _load_open_slots(db)


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
    logger.info("[recheck] printer=%d A%dT%d operator re-check recorded (intent %d)", printer_id, ams_id, tray_id, intent.id)
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


def tag_ness_answered(printer_id: int, ams_id: int, tray_id: int, *, seated: bool, identity_asserted: bool) -> bool:
    """Has a commanded discovery read on this slot come back with NO CHIP?

    The single evidence gate between "the operator clicked" and "the farm may mint tagless".

    Delegates to ``ams_presence.read_answered_no_tag`` — the read economy owns the stamps and
    this module must never re-derive them — passing the GENERAL bare-ness the answer lane
    itself already uses (``ams_presence.on_tray_observations`` hands
    ``close_answered_read`` exactly ``tray_bare=not tray_identity_asserted(...)``). That
    predicate is the caller's own constraint by design, which is why row 5a can keep its
    stricter "neither config nor identity" reading without either caller widening the other:
    row 5a is about a BARE tray under a spent tagged binding, this is about whether the
    firmware found a chip. A third-party PETG roll reports ``tray_type: "PETG"`` and
    ``tag_uid: null`` — configured, not bare — and it is the commonest physical shape on this
    fleet, so a bare-only reading could never answer for it.
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
        logger.debug(
            "[recheck] printer=%d A%dT%d owed read not issued: %s", printer_id, ams_id, tray_id, message
        )
    return ok


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


async def newest_minting_intents(
    db: AsyncSession, slots: list[Slot]
) -> dict[Slot, SlotRecheckIntent]:
    """The most recent MINTING re-check per slot — the half of the offer predicate that is
    about history rather than about the current binding.

    One builder, two entry points (:func:`pending_undo` for a single slot,
    :func:`pending_undo_slots` for the assignments listing), so "which mint did this slot's
    last re-check create?" is asked in exactly one way. Newest-first, first hit wins per
    slot: an older mint on the same slot has long since been superseded.
    """
    if not slots:
        return {}
    wanted = set(slots)
    rows = (
        await db.execute(
            select(SlotRecheckIntent)
            .where(
                SlotRecheckIntent.minted_spool_id.is_not(None),
                SlotRecheckIntent.printer_id.in_({p for p, _, _ in wanted}),
            )
            .order_by(SlotRecheckIntent.id.desc())
        )
    ).scalars()
    newest: dict[Slot, SlotRecheckIntent] = {}
    for row in rows:
        key = (row.printer_id, row.ams_id, row.tray_id)
        if key in wanted and key not in newest:
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

    Everything the restore actually writes belongs to
    ``spool_tagless.replace_bound_row_with_predecessor``, the mirror of that module's existing
    charge-re-attribution lane. Nothing is written here.

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
    if minted is None:  # pragma: no cover — SET NULL makes this unreachable via the FK
        return None, "mint_gone"

    predecessor = (
        await db.execute(last_released_from_slot_stmt(printer_id, ams_id, tray_id).limit(1))
    ).scalar_one_or_none()
    if predecessor is None or predecessor.id == minted.id:
        logger.warning(
            "[recheck] printer=%d A%dT%d undo declined: nothing recorded as last released from this slot",
            printer_id,
            ams_id,
            tray_id,
        )
        return None, "no_predecessor"

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
    await db.commit()
    return predecessor, "restored"
