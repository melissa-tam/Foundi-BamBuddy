"""Spool ↔ AMS-slot binding lifecycle — the single canonical writer (fork farm feature).

**Structural invariant: one spool row is bound to at most ONE AMS slot,
fleet-wide, at every instant.** A physical roll is in exactly one place, so every
re-bind is a MOVE, never a copy: :func:`bind_spool_to_slot` sweeps every existing
binding of the spool (any printer / any AMS / any tray) before creating the new
one, and ``spool_assignment.spool_id`` carries a UNIQUE index
(``ux_spool_assignment_spool_id``, ``models.spool_assignment`` + the
``core.database.run_migrations`` dedupe-then-index block) so a write path that
bypasses this module dies loudly with an IntegrityError instead of silently
forking a ledger.

Incident class this closes (012-H2S, 2026-07-30): the three assignment-creating
sites each deleted only the row on the TARGET slot, so a roll moved tray 0 → tray 1
was recorded as a copy — spool 120 stayed bound to tray 0 for 22 h while also bound
to tray 1. Both trays then presented the same ledger (identical ``inv_g`` and
identical ``loaded_at`` ordinal) to the ``min_start_spool_g`` start gate, the gate
cleared a ~22 g roll as a 167 g one, and the print ran a filament change on layer 1.
Grams from two feeders funnelled into one row (fleet-wide symptom: 29/147 active
spools with ``weight_used > label_weight``).

Home of the three FIFO stamps too: this is now the lowest module every
assignment-creating caller imports (``spool_tag_matcher``, ``spool_tagless``, the
inventory route and ``ams_presence`` all import it; it imports none of them), so the
stamps live beside the writer that fires them with no import cycle and no
function-level import workarounds. Imports here stay strictly downward — models,
sqlalchemy and one leaf util only.

Also home of the UNBIND half of the ledger (:func:`release_spool_from_slot`): an
assignment claims WHERE a roll physically is (doctrine rule 9, operator-ratified
2026-08-01), so a roll leaving a slot must drop the claim — while the row keeps its
grams. The one durable residue of the departure is the ``spool.last_location_*``
stamp this module writes, which is what lets a pulled-and-returned roll reclaim its
gram history on re-insert. The one durable memory it CLEARS is the row's pending
fresh-roll prompt: that question names a slot, and its subject left with the roll.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Select, select
from sqlalchemy.orm import selectinload

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.utils.retry_window import RetryWindow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Consumption below this (grams) counts as "never fed": load-purge noise, not real
# print consumption. A never-fed row holds no seating seniority, so a physical
# re-seat of one re-stamps its ``loaded_at`` (006-H2S FIFO rule 2). A row that has
# fed >= this keeps position on a re-seat (maintenance of the SAME roll — rule 3).
NEVER_FED_MAX_G = 10.0

# Minimum spacing between MOVE binds of ONE roll. A physical roll cannot be in two
# trays, so two moves of the same spool inside this window are a wire artefact, not
# two journeys: sticky identity across partial AMS pushes can present one tag on two
# trays, and each pass then produces two moves (007-H2C, 2026-08-01: spool 194
# ping-ponged AMS1-T0 ⇄ T1, 51 move lines in 5 m 21 s, each one a DB delete+insert, a
# WS broadcast, an un-throttled cali publish and a ``loaded_at`` rewrite that shredded
# the roll's FIFO seniority ~1440×). Keyed on ``spool.id`` ALONE — deliberately not
# per-slot or per-printer, because the flip is BETWEEN slots and can cross printers.
_MOVE_DAMPER_S = 10.0

_move_damper = RetryWindow(_MOVE_DAMPER_S)

# The one origin exempt from the move damper: an operator's explicit assign is a
# statement of fact, not a wire observation, so it is never second-guessed. This is
# the SOLE behavioural effect ``origin`` has — every other use is log attribution.
OPERATOR_ORIGIN = "manual_api"


def _stamp_last_location(spool: Spool, *, printer_id: int, ams_id: int, tray_id: int) -> None:
    """Record where ``spool`` was last physically bound, as of now.

    Written ONLY from this module's two unbind paths (:func:`release_spool_from_slot`
    and :func:`bind_spool_to_slot`'s sweep — a move or a displacement is also a
    release of the OLD location). Documented denormalization: the same fact is in the
    ``[slot-state] … release`` log line this module emits, and that log line is the
    normal-form source, but logs are rotated while the reclaim lane must still work
    weeks later and must stay a join-free column read on the candidate row. NULLs
    mean "never released from a slot" — an in-service or never-bound roll.

    ``last_location_printer_id`` deliberately carries NO foreign key: a deleted
    printer must not cascade away a roll's gram-continuity hint, and the value is a
    historical observation, not a live reference.
    """
    spool.last_location_printer_id = printer_id
    spool.last_location_ams_id = ams_id
    spool.last_location_tray_id = tray_id
    spool.last_location_at = datetime.utcnow()


def last_released_from_slot_stmt(printer_id: int, ams_id: int, tray_id: int) -> Select:
    """Rows whose LAST release was FROM this slot — newest first, unbound fleet-wide.

    The ONE origin for that question, and the read half of the residue
    :func:`_stamp_last_location` above writes: writer and reader live in the same
    module so the shape of "what the departure left behind" cannot drift between them.
    Two lanes ask it, for opposite purposes, and the shared stmt is what keeps them
    answering about the same set of rows:

    * ``slot_pipeline._debounce_candidate`` — a roll came BACK: de-bounce its grams
      and its FIFO position instead of minting a fresh 0 g row (doctrine rule 7);
    * ``spool_respool._mark_tray_spent`` tier 2 — a roll ran OUT: the AMS clears a
      drained slot's exist bit minutes BEFORE it declares the runout, so by the time
      the exhaustion evidence lands the binding is already gone (doctrine rule 9 — the
      bay is empty, the release is correct) and this residue is the only thing left
      that still names the victim.

    ``~assignments.any()`` is part of the shared shape, not a caller's preference: a
    row holding a live assignment is somewhere else NOW, and ``spool_assignment.spool_id``
    is unique, so ANY assignment means "bound elsewhere". Both lanes need that exclusion
    for the same reason (cross-cutting invariant 11, the evidence-tier asymmetry) —
    last-location is ASSUMPTION-tier evidence, and assumption-tier evidence may neither
    steal a roll from a positive location claim nor stamp one the wire says is seated in
    another tray.

    DELIBERATELY carries no spent / archived / fingerprint filter and no LIMIT: those
    are the callers' OWN adjudications and they disagree on purpose. The reclaim lane
    excludes spent and archived donors in SQL and then fingerprint-scans; the spent lane
    must SEE a spent newest row to answer a duplicate trigger idempotently instead of
    walking past it to an older, healthy residue of the same slot. Returning a stmt
    rather than rows is what lets each compose its own answer in one query.
    """
    return (
        select(Spool)
        .where(
            ~Spool.assignments.any(),
            Spool.last_location_printer_id == printer_id,
            Spool.last_location_ams_id == ams_id,
            Spool.last_location_tray_id == tray_id,
        )
        .order_by(Spool.last_location_at.desc())
    )


def stamp_first_loaded(spool: Spool) -> None:
    """Stamp ``Spool.first_loaded_at`` the first time a spool enters service.

    Single stamping origin for the FIFO substrate: called wherever a spool
    acquires its first ``SpoolAssignment`` — i.e. from :func:`bind_spool_to_slot`,
    the one binding writer behind the RFID auto-assign path
    (``spool_tag_matcher.auto_assign_spool``), the manual ``POST /assignments``
    route, and the tagless bare-tray auto-config (``services.spool_tagless``).
    Idempotent: only writes when the column is still NULL, so a spool pulled and
    re-assigned keeps its original in-service timestamp (the spool-selection policy
    orders candidates oldest-first).

    Lives here — not in ``spool_tag_matcher`` / ``spool_tagless`` — because this
    module is the lowest one all assignment-creating callers already import, so
    defining it here is the one clean direction with no import cycle.
    """
    if spool.first_loaded_at is None:
        spool.first_loaded_at = datetime.utcnow()


def stamp_loaded(spool: Spool) -> None:
    """Unconditionally stamp ``Spool.loaded_at`` = now — the re-stampable FIFO ordinal.

    Always writes (unlike write-once :func:`stamp_first_loaded`): callers own the churn
    guard. Binding-change sites call it only when the slot→spool pairing actually changed
    (never on a same-spool upsert replay); the presence-gain adjudicator
    (:func:`stamp_loaded_for_slot`) decides whether a re-seat of the currently-bound row
    qualifies. ``loaded_at`` tracks when the physical roll currently in the tray became
    seated, so the ``first_loaded`` selection policy drains the oldest ACTUAL roll first
    (006-H2S: ``first_loaded_at`` was a write-once ledger row age that a stale binding
    lent to a fresh roll, inverting FIFO).
    """
    spool.loaded_at = datetime.utcnow()


async def stamp_loaded_for_slot(db: AsyncSession, printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Adjudicate a presence-GAIN re-seat and re-stamp ``loaded_at`` when the roll is new.

    Applied to the row CURRENTLY bound to the slot on a debounced genuine presence gain
    (called best-effort from ``ams_presence.on_tray_observations``). The decision is GRAMS-STATE
    + identity only — never elapsed time (the ≥5 s presence filter upstream is a wire
    debounce, not an identity signal):

    * ``tag_uid`` set → skip. RFID identity is adjudicated by the AMS reconcile — a
      same-tag re-seat preserves the binding untouched (position kept); a tag change
      re-binds through ``spool_tag_matcher.auto_assign_spool`` (stamped by
      :func:`bind_spool_to_slot`'s pairing-change guard).
    * ``spent_at`` set → skip. The spent latch owns the slot; the replacement mint is
      stamped at bind time (:func:`stamp_loaded` on the binding change).
    * ``weight_used`` < :data:`NEVER_FED_MAX_G` → stamp. A row that has consumed nothing
      holds no consumption seniority — new-vs-same full roll is ledger-equivalent, so the
      re-seat is the deterministic new-roll answer (006-H2S rule 2, tonight's case).
    * else (mid-life, has fed) → no stamp. The dominant re-seat of a fed roll is
      maintenance (jam fix / untangle / drying) of the SAME roll — position and grams both
      keep; a true mid-life tagless swap is undecidable without a tag / wire remain and is
      left to the ≥70 %-used fresh-roll prompt or manual respool (006-H2S rule 3).

    Commits the shared session on a stamp (commit precedent:
    ``spool_recovery._clear_out_of_rotation_for_slot``). Returns True when it stamped.
    """
    res = await db.execute(
        select(SpoolAssignment)
        .options(selectinload(SpoolAssignment.spool))
        .where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    sa = res.scalar_one_or_none()
    if sa is None or sa.spool is None:
        return False  # unbound slot — nothing to adjudicate
    spool = sa.spool
    if spool.tag_uid:
        return False  # RFID identity is adjudicated by the reconcile, not here
    if spool.spent_at is not None:
        return False  # spent latch owns the slot; the mint stamps at bind time
    if float(spool.weight_used or 0) >= NEVER_FED_MAX_G:
        return False  # mid-life row: a maintenance re-seat keeps position
    stamp_loaded(spool)
    await db.commit()
    logger.info(
        "Re-stamped loaded_at on never-fed spool %d (printer %d AMS%d-T%d re-seat)",
        spool.id,
        printer_id,
        ams_id,
        tray_id,
    )
    return True


async def release_spool_from_slot(db: AsyncSession, assignment: SpoolAssignment, *, reason: str) -> bool:
    """Drop one AMS-slot location claim — the ONE unbind writer.

    An assignment row says WHERE a roll physically is (doctrine rule 9): the roll's
    grams live on the spool row, so releasing a slot costs no ledger history. Every
    deliberate unbind goes through here — the operator's ``DELETE /assignments``
    route and the wire pipeline's release-on-empty transition — so the departure
    always leaves the same two artefacts:

    * ``spool.last_location_*`` stamped from the assignment's own triple
      (:func:`_stamp_last_location`), the durable residue the reclaim lane reads when
      the roll comes back: a pulled-for-drying roll re-inserted into the same slot is
      rebound to its OWN row (grams continue) instead of minting a fresh 0 g one.
    * one ``[slot-state]`` INFO line, the structured grammar the forensics lane
      greps (``support/logs``) — the reason an incident is a query, not a session.

    **The departing row's pending fresh-roll prompt is CLEARED here** (returned as
    ``True`` so the caller can dismiss the toast cross-client). That is a deliberate
    reversal of the replay lane's "skip stale rows without mutating them" choice
    (``spool_tagless.pending_fresh_prompts``): a prompt asks the operator about the roll
    in ONE named slot, and once that roll's location claim is gone the question has no
    subject — the stamp then survived as a toast that replayed for days about a slot the
    roll had left. Nothing durable is lost, because the prompt is a PER-CYCLE contract:
    any return of any roll to that slot raises a qualified physical cycle, which
    re-stamps and re-asks. A release that is really a MOVE (the roll went to another
    slot) clears correctly for the same reason — the question named the slot it left.

    A missing spool row (hand-deleted / cascade race) still releases the slot: the
    location claim is bogus either way, and only the stamp is skipped.

    The caller keeps commit ownership (this only flushes), matching
    :func:`bind_spool_to_slot` and every existing call site's transaction shape.
    """
    printer_id = assignment.printer_id
    ams_id = assignment.ams_id
    tray_id = assignment.tray_id
    spool_id = assignment.spool_id

    prompt_cleared = False
    spool = await db.get(Spool, spool_id)
    if spool is not None:
        _stamp_last_location(spool, printer_id=printer_id, ams_id=ams_id, tray_id=tray_id)
        if spool.fresh_prompt_pending_at is not None:
            spool.fresh_prompt_pending_at = None
            prompt_cleared = True

    await db.delete(assignment)
    await db.flush()

    logger.info(
        "[slot-state] printer=%d A%dT%d release spool=%d reason=%s%s",
        printer_id,
        ams_id,
        tray_id,
        spool_id,
        reason,
        " (fresh-roll prompt cleared)" if prompt_cleared else "",
    )
    return prompt_cleared


async def bind_spool_to_slot(
    db: AsyncSession,
    spool: Spool,
    *,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    fingerprint_color: str | None,
    fingerprint_type: str | None,
    origin: str,
    preserve_ordinal: bool = False,
    bind_moment: datetime | None = None,
) -> SpoolAssignment | None:
    """Bind ``spool`` to one AMS slot with MOVE semantics — the ONE binding writer.

    Every production path that creates a :class:`SpoolAssignment` goes through here
    (RFID auto-assign, tagless setting-seeded bind, manual ``POST /assignments``), so
    the fleet-wide "one spool ⇔ at most one slot" invariant has a single enforcement
    point above the DB's unique index rather than three hand-rolled upserts.

    Sequence:

    1. Read the row currently on the TARGET slot and remember its ``spool_id``: that
       is what distinguishes a genuine binding CHANGE (new roll here → re-stamp the
       FIFO ordinal) from a same-spool upsert replay (re-detect of the roll already
       seated → keep position).
    2. Damp: if this spool already holds a binding on a DIFFERENT slot, the call is a
       MOVE, and a second move of the same roll inside :data:`_MOVE_DAMPER_S` is
       refused (returns None, nothing written) — see the constant for the 007-H2C
       flip-flop storm it exists for. First binds and same-slot re-binds are never
       damped (they are not moves), and neither is ``origin`` :data:`OPERATOR_ORIGIN`.
    3. Sweep: delete the target-slot row AND every other row bound to this spool on
       ANY printer/AMS/tray. A roll that reappears elsewhere MOVED — the old binding
       is stale by physics, and leaving it is exactly the 012-H2S duplicate that fed
       one roll's grams into two trays' ledgers. Every binding-state CHANGE the sweep
       makes leaves an INFO trail AND stamps the departing roll's
       ``last_location_*`` (a move / displacement is also a release of the old slot),
       in both directions: a row of THIS spool elsewhere is logged as a move
       (from→to), and an INCUMBENT different spool on the target slot is logged as a
       displacement (it goes unbound fleet-wide, so silence there would be the one
       unrecorded state change). A same-spool same-slot replay changes nothing: no
       log, no stamp.
    4. Flush BEFORE the insert. SQLAlchemy orders INSERTs before DELETEs within one
       flush, so without this the transient duplicate trips
       ``ux_spool_assignment_spool_id`` (and, on a same-slot replay, the
       ``(printer_id, ams_id, tray_id)`` constraint).
    5. Create + add + flush the new row.
    6. Stamp: :func:`stamp_first_loaded` always (write-once), :func:`stamp_loaded`
       only when the pairing changed and ``preserve_ordinal`` is False.

    ``preserve_ordinal`` is the RECLAIM lane's opt-out of step 6's re-stamp: a roll
    pulled for drying / maintenance and returned is the SAME physical roll, so it
    keeps its FIFO seating position (doctrine rule 7 — a mid-life re-seat keeps
    position). Without it the reclaim would look like a new roll to the
    ``first_loaded`` selector purely because the binding row was rebuilt.

    ``bind_moment`` is its counterpart for the OTHER durable fact a rebuilt row would
    otherwise re-state: ``SpoolAssignment.created_at``. That column is not decoration —
    ``spool_tagless.reconcile_ledger_overcharges`` reads it as the ONE instant at which
    an unobserved roll swap could have happened (meaningfully later than the spool row's
    own ``created_at`` ⇒ the roll left and came back), so every fresh stamp ASSERTS that
    a physical event occurred here. A de-bounce asserts the opposite — the 2026-08-19
    scoping admits it only for a release the farm has just certified as SPURIOUS (rule 7
    as amended) — and a boundary stamped there would split a de-bounced roll that later
    overshoots its label into a phantom successor for a swap that provably did not
    happen. So the de-bounce hands in the moment its roll's CURRENT seating actually
    began (``slot_pipeline._debounce_bind_moment``) instead of minting a new one. None =
    now, via the column's server default, which is what every genuine bind wants.

    Deliberately a separate argument rather than a second meaning for
    ``preserve_ordinal``: the two preserve different facts for different consumers (the
    FIFO selector vs the overcharge reconciler), and one caller passing both is not a
    reason to fuse two contracts into one flag.

    ``origin`` ("rfid_auto" | "tagless_setting" | "manual_api") is log attribution
    plus exactly ONE behavioural carve-out — :data:`OPERATOR_ORIGIN` bypasses the
    move damper, because an operator's explicit assign is a statement of fact rather
    than a wire observation. No other exemption exists.

    Returns the new assignment, or None when the move damper refused the call (the
    DB is untouched — callers must treat None as "no binding change happened").

    The caller keeps commit ownership (this only flushes), matching every call site's
    existing transaction shape.
    """
    existing = await db.execute(
        select(SpoolAssignment).where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    target = existing.scalar_one_or_none()
    old_spool_id = target.spool_id if target is not None else None

    prior = await db.execute(select(SpoolAssignment).where(SpoolAssignment.spool_id == spool.id))
    doomed: list[SpoolAssignment] = list(prior.scalars().all())
    if target is not None and all(row is not target for row in doomed):
        doomed.append(target)

    # A row of this spool on another slot is the ONLY thing that makes this a move —
    # evaluate the damper on nothing else, so a first bind and a same-slot re-bind
    # can never be suppressed (they carry no flip-flop risk and refusing them would
    # simply lose a state change).
    is_move = any(row is not target for row in doomed)
    if is_move and origin != OPERATOR_ORIGIN and not _move_damper.allow(spool.id):
        logger.warning(
            "[slot-state] damped move: spool %d -> printer %d A%dT%d (origin=%s) — another move of this "
            "roll landed within %.0fs; a roll cannot be in two trays, so this is wire churn",
            spool.id,
            printer_id,
            ams_id,
            tray_id,
            origin,
            _MOVE_DAMPER_S,
        )
        return None

    for row in doomed:
        if row is not target:
            _stamp_last_location(spool, printer_id=row.printer_id, ams_id=row.ams_id, tray_id=row.tray_id)
            logger.info(
                "spool %d moved: unbound from printer %d AMS%d-T%d -> printer %d AMS%d-T%d (origin=%s)",
                spool.id,
                row.printer_id,
                row.ams_id,
                row.tray_id,
                printer_id,
                ams_id,
                tray_id,
                origin,
            )
        elif old_spool_id != spool.id:
            # The incumbent of the target slot is a DIFFERENT roll: it loses its only
            # binding and goes unbound fleet-wide. Logged so the one binding-state
            # change with no destination still leaves a trail, and stamped so the
            # evicted roll can still reclaim its grams if it is re-inserted (a same-
            # spool replay, old_spool_id == spool.id, is a non-event: neither).
            incumbent = await db.get(Spool, old_spool_id)
            if incumbent is not None:
                _stamp_last_location(incumbent, printer_id=printer_id, ams_id=ams_id, tray_id=tray_id)
            logger.info(
                "spool %d displaced: unbound from printer %d AMS%d-T%d by spool %d (origin=%s)",
                old_spool_id,
                printer_id,
                ams_id,
                tray_id,
                spool.id,
                origin,
            )
        await db.delete(row)
    # Mandatory: the unit of work would otherwise INSERT the new row before these
    # DELETEs land and trip a unique constraint on the transient duplicate.
    await db.flush()

    assignment = SpoolAssignment(
        spool_id=spool.id,
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        fingerprint_color=fingerprint_color,
        fingerprint_type=fingerprint_type,
    )
    if bind_moment is not None:
        # Carried forward, not re-stamped — see the ``bind_moment`` paragraph above.
        assignment.created_at = bind_moment
    db.add(assignment)
    await db.flush()

    # First-in-service stamp (FIFO substrate). Idempotent — only the first
    # assignment sets it; a spool pulled and re-assigned keeps its timestamp.
    stamp_first_loaded(spool)
    # Re-stampable FIFO ordinal: a binding CHANGE to a different spool row (RFID
    # auto-assign, tagless mint, respool re-bind, from-slot route, manual assign —
    # all new-row callers) is a reliable novelty event, so the seating order rides
    # it. A same-spool upsert replay keeps position, and so does an explicit
    # ``preserve_ordinal`` reclaim (the SAME roll returning — doctrine rule 7).
    if old_spool_id != spool.id and not preserve_ordinal:
        stamp_loaded(spool)

    return assignment
