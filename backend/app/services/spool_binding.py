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
function-level import workarounds. Imports here stay strictly downward — models and
sqlalchemy only.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Consumption below this (grams) counts as "never fed": load-purge noise, not real
# print consumption. A never-fed row holds no seating seniority, so a physical
# re-seat of one re-stamps its ``loaded_at`` (006-H2S FIFO rule 2). A row that has
# fed >= this keeps position on a re-seat (maintenance of the SAME roll — rule 3).
NEVER_FED_MAX_G = 10.0


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
    (called best-effort from ``ams_presence.on_ams_change``). The decision is GRAMS-STATE
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
) -> SpoolAssignment:
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
    2. Sweep: delete the target-slot row AND every other row bound to this spool on
       ANY printer/AMS/tray. A roll that reappears elsewhere MOVED — the old binding
       is stale by physics, and leaving it is exactly the 012-H2S duplicate that fed
       one roll's grams into two trays' ledgers. Every binding-state CHANGE the sweep
       makes leaves an INFO trail, in both directions: a row of THIS spool elsewhere
       is logged as a move (from→to), and an INCUMBENT different spool on the target
       slot is logged as a displacement (it goes unbound fleet-wide, so silence there
       would be the one unrecorded state change). A same-spool same-slot replay
       changes nothing and stays silent.
    3. Flush BEFORE the insert. SQLAlchemy orders INSERTs before DELETEs within one
       flush, so without this the transient duplicate trips
       ``ux_spool_assignment_spool_id`` (and, on a same-slot replay, the
       ``(printer_id, ams_id, tray_id)`` constraint).
    4. Create + add + flush the new row.
    5. Stamp: :func:`stamp_first_loaded` always (write-once), :func:`stamp_loaded`
       only when the pairing changed.

    ``origin`` ("rfid_auto" | "tagless_setting" | "manual_api") is log attribution
    only — it never changes behaviour, so no path can quietly earn an exemption.

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

    for row in doomed:
        if row is not target:
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
            # change with no destination still leaves a trail (a same-spool replay,
            # old_spool_id == spool.id, is a non-event and stays silent).
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
    db.add(assignment)
    await db.flush()

    # First-in-service stamp (FIFO substrate). Idempotent — only the first
    # assignment sets it; a spool pulled and re-assigned keeps its timestamp.
    stamp_first_loaded(spool)
    # Re-stampable FIFO ordinal: a binding CHANGE to a different spool row (RFID
    # auto-assign, tagless mint, respool re-bind, from-slot route, manual assign —
    # all new-row callers) is a reliable novelty event, so the seating order rides
    # it. A same-spool upsert replay keeps position.
    if old_spool_id != spool.id:
        stamp_loaded(spool)

    return assignment
