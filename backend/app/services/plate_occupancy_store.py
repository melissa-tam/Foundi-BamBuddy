"""The I/O half of the plate-occupancy authority: the loader and the side effects (WS2).

``plate_occupancy`` is I/O-free by contract — stdlib only, no session, no ``await``,
no imports from ``backend.app``. Everything it needs from the world therefore lives
HERE, on the other side of the dependency inversion: one :func:`hydrate` that rebuilds
the durable facts at startup, and the callables the core fans a completed transition
out through (:func:`wire_core` injects them at lifespan).

**Hydration replaces three loaders, whose semantics are ported, not re-invented:**

* ``printer_manager.load_awaiting_plate_clear_from_db`` — the plate itself, from
  ``printers.awaiting_plate_clear`` / ``printers.plate_gate_subtask_id``;
* ``eject.remote.hydrate_pending_ejects_from_db`` — the pending eject, from
  ``print_queue.eject_dispatched_at``, 24 h TTL, one per printer, newest wins;
* ``eject.monitor.rearm_on_startup`` — which of those plates may auto-clear, decided
  by the SAME :func:`~backend.app.services.eject.monitor.should_rearm` rule (imported,
  never re-spelled) and expressed now as a POLICY on the plate rather than a spawned
  watch.

No lease is ever hydrated: a lease is process-local by construction, and the durable
witness that a unit is already on its way is the ``printing`` queue row the scheduler
re-derives per tick as ``Evidence.db_claim``.

**Ordering inside :func:`hydrate` is load-bearing.** Ejects are rebuilt FIRST so that
the plate's policy decision — and the policy driver's first view of that plate — can
both see an eject the startup reconciler owns. A cooldown policy armed over a plate
that already carries a pending eject is the double-dispatch the legacy rearm avoided
by skipping such printers outright (``monitor.py:898-906``); here it degrades to
:class:`~backend.app.services.plate_occupancy.EscalationOnly` instead of to nothing,
because the never-armless floor forbids a gate with no policy at all.

**Scheduling.** The core calls these callables SYNCHRONOUSLY from inside ``_notify``,
so each one owns its own scheduling and must return immediately. The loop guard is
the idiom at ``printer_manager.py:371`` (``if loop and loop.is_running()``), expressed
here as a lazy :func:`asyncio.get_running_loop` — with no running loop (a synchronous
unit test, a call from a non-loop thread) the I/O is skipped rather than raising, which
is what keeps the core's synchronous transition surface callable from anywhere. The
coroutine is built by a factory so that a skipped schedule never constructs one (an
unawaited coroutine is a warning, and a noisy one at that).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.tasks import spawn_background_task
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.plate_occupancy import (
    CooldownEject,
    EscalationOnly,
    OccupancyPolicy,
    OccupancyView,
    PendingEject,
    plate_occupancy,
)
from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)

# A durable eject stamp older than this is not evidence of an eject in flight — it is
# residue from a crash days ago. Ported verbatim from ``eject.remote`` (whose twin is
# deleted with ``hydrate_pending_ejects_from_db`` at cut-over).
_PENDING_EJECT_STALE_TTL_H = 24

# What the operator is told when a plate ends up with no policy the farm can arm. The
# page is the last rung of the never-armless ladder (2026-07-18/07-21), reached only
# after the core has already tried the escalation-only floor.
_ESCALATION_SOURCE_DETAIL = "occupancy policy failed to arm"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def wire_core() -> None:
    """Inject this module's side effects into the authority. Called once at lifespan.

    ``policy_driver`` is deliberately NOT wired here: the driver is the eject cooldown
    monitor's, and :meth:`~backend.app.services.plate_occupancy.PlateOccupancy.configure`
    leaves an omitted callable untouched, so the two lanes can wire independently.
    """
    plate_occupancy.configure(
        persist=persist_occupancy,
        broadcast=broadcast_occupancy,
        kick=kick_scheduler,
        escalate=escalate_occupancy,
    )


# ---------------------------------------------------------------------------
# Startup hydration
# ---------------------------------------------------------------------------


async def hydrate() -> None:
    """Rebuild every durable occupancy fact into the authority. Once, at lifespan.

    Each half swallows its own failure with a WARNING (the legacy plate loader's
    behaviour) so that a broken eject stamp cannot cost the fleet its plate gates —
    losing a gate is what dispatches a unit onto an occupied plate.
    """
    await _hydrate_ejects()
    await _hydrate_plates()


async def _hydrate_ejects() -> int:
    """Rebuild pending ejects from ``print_queue.eject_dispatched_at``. Returns the count.

    Port of ``eject.remote.hydrate_pending_ejects_from_db``, decision for decision:
    newest stamp first; a stamp past the TTL is dropped AND NULLed with a WARNING; a
    stamp with no ``printer_id`` likewise; a second stamp for a printer that already has
    one is NULLed, because the record is one-per-printer by construction. ``purpose`` is
    the item's own ``first_article`` flag — a MANUAL eject carries no queue item, so it
    has no durable mirror and can never be hydrated (the prior ruling: a mid-eject
    restart degrades to an escalation-only hold).

    ``dispatched_at`` is carried into the record so the operator sees the eject's true
    age rather than the age of this process; the core forces ``hydrated=True`` whatever
    is passed, which is what disables the watchdog and lets an operator supersede it.
    """
    from backend.app.core.database import async_session

    cutoff = datetime.now(timezone.utc) - timedelta(hours=_PENDING_EJECT_STALE_TTL_H)
    hydrated = 0
    try:
        async with async_session() as db:
            result = await db.execute(
                select(PrintQueueItem)
                .where(PrintQueueItem.eject_dispatched_at.is_not(None))
                .order_by(PrintQueueItem.eject_dispatched_at.desc())
            )
            rows = list(result.scalars().all())
            seen_printers: set[int] = set()
            changed = False
            for item in rows:
                stamp = _as_utc(item.eject_dispatched_at)
                if stamp is None or stamp < cutoff:
                    logger.warning(
                        "[occupancy-store] dropping stale pending-eject stamp on item %s (dispatched %s, TTL %sh)",
                        item.id,
                        item.eject_dispatched_at,
                        _PENDING_EJECT_STALE_TTL_H,
                    )
                    item.eject_dispatched_at = None
                    changed = True
                    continue
                if item.printer_id is None:
                    logger.warning("[occupancy-store] dropping pending-eject stamp on item %s — no printer_id", item.id)
                    item.eject_dispatched_at = None
                    changed = True
                    continue
                if item.printer_id in seen_printers:
                    logger.warning(
                        "[occupancy-store] multiple pending ejects for printer %s — NULLing older stamp on item %s",
                        item.printer_id,
                        item.id,
                    )
                    item.eject_dispatched_at = None
                    changed = True
                    continue
                seen_printers.add(item.printer_id)
                plate_occupancy.hydrate_eject(
                    item.printer_id,
                    PendingEject(
                        purpose="fa" if item.first_article else "production",
                        run_id=item.batch_id,
                        queue_item_id=item.id,
                        dispatched_at=stamp,
                    ),
                )
                hydrated += 1
            if changed:
                await db.commit()
    except Exception:
        logger.warning("[occupancy-store] pending-eject hydration failed", exc_info=True)
    if hydrated:
        logger.info("[occupancy-store] hydrated %d pending eject(s) from durable stamps", hydrated)
    return hydrated


async def _hydrate_plates() -> int:
    """Rebuild occupied plates and choose each one's startup policy. Returns the count."""
    from backend.app.core.database import async_session

    hydrated: list[int] = []
    cooldowns = 0
    try:
        async with async_session() as db:
            result = await db.execute(
                select(Printer.id, Printer.plate_gate_subtask_id, Printer.quarantined).where(
                    Printer.awaiting_plate_clear.is_(True)
                )
            )
            for printer_id, gate_subtask_id, quarantined in result.all():
                policy = await _startup_policy(db, printer_id, gate_subtask_id, bool(quarantined))
                plate_occupancy.hydrate_plate(printer_id, source_subtask_id=gate_subtask_id, policy=policy)
                hydrated.append(printer_id)
                if isinstance(policy, CooldownEject):
                    cooldowns += 1
    except Exception:
        logger.warning("[occupancy-store] plate hydration failed", exc_info=True)
    if hydrated:
        logger.info(
            "[occupancy-store] hydrated %d occupied plate(s) %s — %d cooldown-re-armed, %d escalation-only",
            len(hydrated),
            sorted(hydrated),
            cooldowns,
            len(hydrated) - cooldowns,
        )
    return len(hydrated)


async def _startup_policy(
    db: AsyncSession, printer_id: int, gate_subtask_id: str | None, quarantined: bool
) -> OccupancyPolicy:
    """What happens next to a plate that survived a restart. The re-arm ladder.

    Ports ``monitor.rearm_on_startup``'s decision (``monitor.py:872-934``) into a
    policy, with :func:`~backend.app.services.eject.monitor.should_rearm` still the one
    spelling of the rule itself. A cooldown eject is re-armed ONLY when every rung
    holds: the printer is not quarantined, no pending eject is registered on it, the
    most-recently-started unit there completed successfully with an eject profile and is
    not a first article, and the gate's source id positively names THAT unit's dispatch.

    Everything else is :class:`~backend.app.services.plate_occupancy.EscalationOnly` —
    including the two rungs the legacy code expressed by arming nothing at all (a
    quarantined printer was filtered out of the query; a printer with a hydrated pending
    eject was skipped as "the reconciler owns it"). Under the authority a gate is always
    attached to a policy, so those become an escalation hold rather than silence: the
    plate still never auto-clears, but it now escalates until a human clears it.

    A first article never re-arms even though it carries a profile: the part holds on
    the plate for inspection and the approval flow arms its own FA eject.
    """
    from backend.app.services.eject.monitor import should_rearm

    if quarantined:
        logger.info(
            "[occupancy-store] p%s gate held on a QUARANTINED printer — escalation-only (no auto-clear)", printer_id
        )
        return EscalationOnly()

    if plate_occupancy.eject_identity(printer_id) is not None:
        logger.info(
            "[occupancy-store] p%s has a hydrated pending eject — the startup reconciler owns it; escalation-only",
            printer_id,
        )
        return EscalationOnly()

    item = await _latest_started_item(db, printer_id)
    cooldown_rearmable = (
        item is not None
        and should_rearm(True, item.status, item.eject_profile_id, bool(item.first_article))
        and bool(gate_subtask_id)
        and item.dispatch_subtask_id == gate_subtask_id
    )
    if cooldown_rearmable and item is not None:
        logger.info("[occupancy-store] p%s gate cooldown-re-armed from unit %s", printer_id, item.id)
        return CooldownEject(unit_id=item.id, run_id=item.batch_id)

    logger.info(
        "[occupancy-store] p%s gate NOT cooldown-re-armed (item=%s, gate=%r, last dispatch=%r) "
        "— escalation-only hold (no auto-clear)",
        printer_id,
        item.id if item is not None else None,
        gate_subtask_id,
        item.dispatch_subtask_id if item is not None else None,
    )
    return EscalationOnly()


async def _latest_started_item(db: AsyncSession, printer_id: int) -> PrintQueueItem | None:
    """The most-recently-started queue unit on ``printer_id``, or None.

    The re-arm ladder's subject: the gate's source id must positively name THIS unit's
    dispatch, so a foreign or screen-started print that finished after the farm unit can
    never lend its identity to the wrong plate.
    """
    result = await db.execute(
        select(PrintQueueItem)
        .where(PrintQueueItem.printer_id == printer_id)
        .where(PrintQueueItem.started_at.is_not(None))
        .order_by(PrintQueueItem.started_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Injected side effects
# ---------------------------------------------------------------------------


def persist_occupancy(printer_id: int, view: OccupancyView) -> None:
    """Mirror the record's DURABLE half into the DB: the plate columns and the eject stamp.

    Two writes, one transaction, because they are one fact: what this printer's
    occupancy will be rebuilt as after a restart.

    * the plate — ``printers.awaiting_plate_clear`` plus ``plate_gate_subtask_id``,
      NULLed on release so a later re-arm cannot mistake a stale id for a live gate
      (the body of ``printer_manager._persist_awaiting_plate_clear``);
    * the eject — ``print_queue.eject_dispatched_at`` on the owning unit, or a
      printer-scoped NULL when the record holds nothing durable (the body of
      ``eject.remote.persist_pending_eject`` / ``clear_pending_eject``).

    "Nothing durable" is deliberately WIDER than "no eject": a MANUAL eject is
    memory-only by prior ruling (a mid-eject restart degrades to an escalation-only
    hold), and a manual claim can now SUPERSEDE a hydrated pending — so leaving that
    pending's stamp behind would re-hydrate a phantom eject at the next restart. The
    durable mirror always equals what the record can honestly express.

    The view cannot carry the eject's ``queue_item_id`` / ``dispatched_at`` (it is a
    projection, not the record), so the identity is read back from the authority
    SYNCHRONOUSLY here — same thread, same instant as the fan-out — and only the
    resulting scalars are handed to the scheduled write.
    """
    identity = plate_occupancy.eject_identity(printer_id)
    stamp: tuple[int, datetime] | None = None
    if (
        view.eject_present
        and identity is not None
        and identity.purpose != "manual"
        and identity.queue_item_id is not None
        and identity.dispatched_at is not None
    ):
        stamp = (identity.queue_item_id, identity.dispatched_at)

    plate_occupied = view.plate_occupied
    plate_source = view.plate_source_subtask_id
    _schedule(
        lambda: _write_occupancy(
            printer_id,
            plate_occupied=plate_occupied,
            plate_source_subtask_id=plate_source,
            stamp=stamp,
        ),
        name=f"occupancy-persist-p{printer_id}",
    )


async def _write_occupancy(
    printer_id: int,
    *,
    plate_occupied: bool,
    plate_source_subtask_id: str | None,
    stamp: tuple[int, datetime] | None,
) -> None:
    """The scheduled half of :func:`persist_occupancy`. Retried, and never fatal.

    ``run_with_retry`` is the fork's SQLite single-writer cure (a fresh session per
    attempt); a failure is a WARNING and nothing else, because an occupancy fact that
    already happened must not be unwound by a DB hiccup.
    """
    from backend.app.core.database import run_with_retry

    async def _do(db: AsyncSession) -> None:
        printer = await db.get(Printer, printer_id)
        if printer is not None:
            printer.awaiting_plate_clear = plate_occupied
            # The gate's source is meaningful only while it is raised.
            printer.plate_gate_subtask_id = plate_source_subtask_id if plate_occupied else None
        await _apply_eject_stamp(db, printer_id, stamp)
        await db.commit()

    try:
        await run_with_retry(_do, label=f"persist occupancy printer={printer_id}")
    except Exception as e:
        logger.warning("[occupancy-store] failed to persist occupancy for printer %d: %s", printer_id, e)


async def _apply_eject_stamp(db: AsyncSession, printer_id: int, stamp: tuple[int, datetime] | None) -> None:
    """Stamp the owning unit, or NULL every stamp on this printer. Idempotent either way.

    The clear is printer-scoped rather than keyed on one item id (``clear_pending_eject``'s
    rule) so a crash that stamped two rows for one printer cannot leave an orphan behind
    to hydrate as a phantom eject.
    """
    if stamp is not None:
        item_id, dispatched_at = stamp
        item = await db.get(PrintQueueItem, item_id)
        if item is None:
            return
        if _as_utc(item.eject_dispatched_at) != _as_utc(dispatched_at):
            item.eject_dispatched_at = dispatched_at
        return

    result = await db.execute(
        select(PrintQueueItem).where(
            PrintQueueItem.printer_id == printer_id,
            PrintQueueItem.eject_dispatched_at.is_not(None),
        )
    )
    for item in result.scalars().all():
        item.eject_dispatched_at = None


def broadcast_occupancy(printer_id: int) -> None:
    """Push the printer's status over the websocket after a Bambuddy-side state change.

    Occupancy is not an MQTT fact, so toggling it produces no printer push: without this
    emit every subscriber that is not the originating tab stays stale until a
    coincidental status refresh (the #1128 reason ``_broadcast_status_change`` exists).
    """
    _schedule(
        lambda: printer_manager._broadcast_status_change(printer_id),
        name=f"occupancy-broadcast-p{printer_id}",
    )


def kick_scheduler(printer_id: int, cause: str) -> None:
    """Wake the scheduler on a release edge, so a freed printer dispatches in ~1 s.

    No loop guard: ``dispatch_kick.kick`` is synchronous and safe from any thread by
    contract, and it is level-triggered — an early kick with no waiter yet is carried
    forward rather than lost. The reason stays the established
    ``plate_gate_release`` vocabulary (the core's *cause* names the transition and is
    logged, not published, so the scheduler's wake-reason ring keeps one spelling).
    """
    try:
        from backend.app.services.dispatch_kick import dispatch_kick

        dispatch_kick.kick("plate_gate_release", printer_id)
    except Exception:
        logger.debug("[occupancy-store] dispatch kick failed on release edge (%s, non-fatal)", cause, exc_info=True)


def escalate_occupancy(printer_id: int, error: BaseException) -> None:
    """Page a human: this plate could not be given a policy, not even the floor.

    The last rung of the never-armless ladder from 2026-07-18/07-21. By the time the
    core calls this it has already repaired the record to ``EscalationOnly`` and failed
    to arm THAT too, so the farm has run out of ways to look after the plate itself.
    """
    logger.warning(
        "[occupancy-store] p%d: %s (%s) — paging plate-not-empty",
        printer_id,
        _ESCALATION_SOURCE_DETAIL,
        error,
    )
    _schedule(lambda: _page_plate_not_empty(printer_id), name=f"occupancy-escalate-p{printer_id}")


async def _page_plate_not_empty(printer_id: int) -> None:
    """Fire the plate-not-empty notification, reusing the monitor's one implementation."""
    from backend.app.services.eject.monitor import notify_plate_not_empty

    try:
        await notify_plate_not_empty(printer_id, source_detail=_ESCALATION_SOURCE_DETAIL)
    except Exception:
        logger.warning("[occupancy-store] p%d: plate-not-empty escalation failed", printer_id, exc_info=True)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _schedule(factory: Callable[[], Coroutine[Any, Any, None]], *, name: str) -> None:
    """Run an async side effect off a synchronous transition, or skip it silently.

    The guard is ``printer_manager.py:371`` in its lazy form: with no running loop there
    is nothing to schedule onto, and raising would let a failed side effect unwind a
    state change that already happened. The coroutine is built only once a loop is
    confirmed — constructing and dropping one emits "coroutine was never awaited", which
    is exactly the noise sync unit tests would drown in.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("[occupancy-store] no running loop — %s skipped", name)
        return
    spawn_background_task(factory(), name=name)


def _as_utc(value: datetime | None) -> datetime | None:
    """Read a stamp back as timezone-aware UTC.

    ``eject_dispatched_at`` is a plain ``DateTime`` column, so SQLite hands it back
    naive; UTC is what every writer put in it. Without this, a stamp read from the DB
    and a stamp minted in memory cannot even be compared.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
