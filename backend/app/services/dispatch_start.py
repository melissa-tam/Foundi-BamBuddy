"""A dispatch's START: its send, the record's answer applied to its claim, and production's self-heal bounds
behind it (owner 9, 2026-10-11; R17/R18 of the 2026-10-12 restoration).

The dispatcher marks a unit ``printing`` and publishes its ``print_start`` through the record
(:func:`send_start` → ``services/printer_action``): that action row is the dispatch's durable send record,
and what the printer makes of it is the record's answer, read on EVENTS (``command_outcome``'s start
family). What an answer means is the leaf ``dispatch_claim``'s; this module APPLIES it.

Events decide first; production's ``_watchdog_print_start`` (``337f4954``) survives as two self-heal bounds
the record counts in the printer's ONLINE seconds — the fallback for a printer that stays silent (operator
ruling 2026-10-12: "The behaviour should stay the same for the most part, im not asking to change
behaviour, i was asking to not rely on timers only as it's finnikey. so if it was self heal, it should stay
self heal."):

* :func:`on_action_answered` — the hub subscriber (``ActionAnswered``). A REACHED start (an ACTIVE word
  carrying the dispatched job's id, or that job's terminal) releases the printer's dispatch lease; a
  REFUSED, VOID or TAKEN-OVER start returns the unit to the queue next in line (``requeue.return_to_queue``
  — through ``queue_transitions.release_unstarted_claim``, the one writer of printing → pending) and
  releases the lease. An answer that comes after the start's bound already returned the unit is LATE
  (``dispatch_claim.start_late``): no claim and no lease is touched, and a print it reached runs as the
  FOREIGN path's — production's #1150 risk, kept.
* :data:`_START_SELF_HEAL` — Phase A: no answer and no landing within 90 s of the send → the unit is
  returned (``start_unanswered``, :func:`on_self_heal_due`). Armed at the send (:func:`send_start`).
* :data:`_START_ACTIVE_SELF_HEAL` — Phase B: the printer reported the job's id at IDLE / FINISH (the record's
  ``Reading.LANDED``) but no active word followed within 180 s of that landing → the unit is returned
  (``start_not_active``) with NO reconnect (#1150: a reconnect mid-parse triggers ``0500_4003``). The record's
  bound is re-anchored at the landing by :func:`on_status_reported`, once per start.
* :func:`watch_start` — the per-dispatch watch (liveness only; it decides nothing about the claim): the
  dispatch-progress phases the queue page shows, and the TRANSPORT check at the Phase A bound — a start
  still unacknowledged on a printer whose ``gcode_file`` never changed is a half-broken MQTT session
  (#887/#936), rebuilt ONCE. The VOID the rebuilt session answers finds the unit already returned (late),
  or returns it itself: whichever comes first wins, and the return is a conditional transition.
* :func:`reevaluate_session` — the session start's step (``session_start``): a start the record answered
  before this process could apply it is applied; an OUTSTANDING start is left to its event — the answer,
  or its bound's ``SelfHealDue`` — unless its bound already passed before a restart (then its fallback is
  taken now); a claim with no start on the record is judged against the printer's own report
  (``dispatch_claim.judge``).

After a restart no watch exists: a rehydrated outstanding start re-arms :data:`_START_SELF_HEAL` through its
bound factory (:func:`rehydrated_start_bound`, registered by ``printer_event_wiring``), counted from the
printer's first fresh report in the new process, and its ``SelfHealDue`` returns the unit with no lane
coroutine alive.

Process memory and its rehydrate story: :data:`_landed_armed` (printer → the starts whose landing bound is
armed). None is needed: after a restart the record re-reads each rehydrated start at the printer's first
fresh report (``command_outcome.Running.start_landed`` set again), and the next status push re-arms the
landing bound from that moment — the printer's online clock starts there anyway.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import dispatch_claim, printer_action
from backend.app.services.command_outcome import Answer, Reading
from backend.app.services.eject import progress as dispatch_progress
from backend.app.services.plate_occupancy import plate_occupancy
from backend.app.services.printer_manager import printer_manager

if TYPE_CHECKING:
    from backend.app.services.printer_events import ActionAnswered, SelfHealDue, StatusReported

logger = logging.getLogger(__name__)

# PHASE A (restored 2026-10-12): how long a dispatched start may go with no answer and no landing — the
# printer reports neither an active word nor the dispatched job's id — before the farm returns the unit.
# Production's ``_watchdog_print_start`` Phase A ``timeout`` (``dispatch_claim.DISPATCH_START_BUDGET_S`` at
# 337f4954), raised 45 → 90 s by #1078 for slow transitions that emit no early ``subtask_id`` tick. A
# printer that received ``project_file`` ACKs it at once and reports the new id within seconds, so a
# healthy start never reaches it. One constant, one clock: the record's.
_START_SELF_HEAL = printer_action.SelfHealBound(
    "_START_SELF_HEAL",
    90.0,
    "production _watchdog_print_start Phase A at 337f4954 (DISPATCH_START_BUDGET_S); #1078 raised it 45 -> 90 s",
)

# PHASE B (restored 2026-10-12): how long a LANDED start — the printer reports the dispatched job's id at
# IDLE / FINISH — may go without an active word before the farm returns the unit. Production's Phase B
# (180 s, entered only when Phase A exited on the ``subtask_id`` advance alone): the H2D sits at FINISH
# ~50 s after taking ``project_file`` before PREPARE (#1078), and a printer that accepts the file but stays
# IDLE/FINISH indefinitely (#1678) used to strand the unit as ``printing``. No reconnect follows it: the file
# landed, and a forced reconnect mid-parse triggers ``0500_4003`` (#1150).
_START_ACTIVE_SELF_HEAL = printer_action.SelfHealBound(
    "_START_ACTIVE_SELF_HEAL",
    180.0,
    "production _watchdog_print_start Phase B at 337f4954 (180 s after the subtask_id advance); "
    "#1678 printer took project_file and stayed IDLE/FINISH",
)

_START_BOUNDS: frozenset[str] = frozenset({_START_SELF_HEAL.name, _START_ACTIVE_SELF_HEAL.name})

# The push cadence the watch reads the printer at (UI phases; the transport check). A cadence, never a
# decision.
_WATCH_POLL_S = 1.0

# printer_id -> the outstanding starts whose landing bound (:data:`_START_ACTIVE_SELF_HEAL`) is armed.
_landed_armed: dict[int, set[int]] = {}


async def send_start(printer_id: int, command: printer_action.Command, *, queue_item_id: int) -> printer_action.Sent:
    """Record and publish a dispatch's ``print_start`` (``printer_action.send``) — a FARM action naming its
    unit, with :data:`_START_SELF_HEAL` armed at the send (the record counts it from ``sent_at``)."""
    return await printer_action.send(
        printer_id,
        command,
        actor=printer_action.ACTOR_FARM,
        queue_item_id=queue_item_id,
        bound=_START_SELF_HEAL,
    )


def rehydrated_start_bound(action: printer_action.OutstandingAction) -> tuple[printer_action.SelfHealBound, datetime]:
    """BOUND FACTORY (``printer_event_wiring``, for ``print_start``): a start rehydrated after a restart gets
    :data:`_START_SELF_HEAL` anchored at its send — the record counts it from the printer's first fresh
    report in this process (``printer_action.online_seconds_since``). A start the printer reports landed is
    re-armed on that landing (:func:`on_status_reported`)."""
    return _START_SELF_HEAL, action.sent_at


def on_status_reported(event: StatusReported) -> None:
    """HUB SUBSCRIBER (``StatusReported``, right after the record's own reading of the push): a start the
    record now reads LANDED (``Reading.LANDED`` — the dispatched job's id at IDLE / FINISH) has its bound
    re-anchored ONCE at this landing, as :data:`_START_ACTIVE_SELF_HEAL` (production Phase B). In memory,
    O(outstanding actions); a printer with no outstanding start costs nothing more. Never raises."""
    try:
        pid = event.printer_id
        starts = {
            action.action_id
            for action in printer_action.outstanding_actions()
            if action.printer_id == pid and action.command == "print_start"
        }
        armed = _landed_armed.setdefault(pid, set())
        armed &= starts  # an answered or vanished start leaves the set
        for action_id in sorted(starts - armed):
            if printer_action.reading_of(action_id) is not Reading.LANDED:
                continue
            armed.add(action_id)
            if printer_action.arm_bound(action_id, _START_ACTIVE_SELF_HEAL, anchor_at=printer_action.now()):
                logger.info(
                    "dispatch_start: printer %s start (action %s) landed — the job's id is reported but the job "
                    "is not active; it is returned if no active word follows within %.0fs online (#1678)",
                    pid,
                    action_id,
                    _START_ACTIVE_SELF_HEAL.seconds,
                )
        if not armed:
            _landed_armed.pop(pid, None)
    except Exception:  # noqa: BLE001 — the status chain must keep running (invariant 10)
        logger.exception("dispatch_start: the landing check failed for printer %s", event.printer_id)


async def on_self_heal_due(event: SelfHealDue) -> None:
    """HUB SUBSCRIBER (``SelfHealDue``): a dispatch's start passed its self-heal bound with no answer —
    production's fallback, with or without a lane coroutine alive: the unit goes back to the queue
    (:func:`_take_fallback`). The action stays outstanding; a late answer then touches nothing
    (``dispatch_claim.start_late``). Never raises."""
    if event.command != "print_start" or event.bound_name not in _START_BOUNDS:
        return
    try:
        bound = _START_ACTIVE_SELF_HEAL if event.bound_name == _START_ACTIVE_SELF_HEAL.name else _START_SELF_HEAL
        # A landing first read on the very push that crossed Phase A cannot re-arm a passed bound: production's
        # Phase A loop returned the unit there too, and the file landed, so the cause is the landing's.
        landed = bound is _START_ACTIVE_SELF_HEAL or printer_action.reading_of(event.action_id) is Reading.LANDED
        await _take_fallback(event.printer_id, event.action_id, item_id=event.queue_item_id, landed=landed, bound=bound)
    except Exception:  # noqa: BLE001 — the hub must keep delivering
        logger.exception("dispatch_start: the start bound of action %s failed", event.action_id)


async def _take_fallback(
    printer_id: int,
    action_id: int,
    *,
    item_id: int | None,
    landed: bool,
    bound: printer_action.SelfHealBound | None,
) -> bool:
    """Return the unit of a start whose bound passed with no answer — when the start is still outstanding
    (an event that answered first is applied by :func:`on_action_answered`, and wins) and still its unit's
    live start (``printing``, and no newer dispatch's start names it). Returns True when the row moved."""
    if not printer_action.outstanding(action_id):
        return False
    from backend.app.core.database import async_session

    if item_id is None:
        item_id = await printer_action.unit_of(action_id)
        if item_id is None:
            return False
    async with async_session() as db:
        status = await db.scalar(select(PrintQueueItem.status).where(PrintQueueItem.id == item_id))
        current = await printer_action.last_start_of(db, item_id)
    if status != "printing" or current != action_id:
        logger.info(
            "dispatch_start: printer %s start (action %s) passed its bound — unit %s is no longer on it "
            "(status %r, newest start %s); nothing to return",
            printer_id,
            action_id,
            item_id,
            status,
            current,
        )
        return False
    cause = dispatch_claim.fallback_cause(landed=landed)
    situation = "the printer reported the job but never began it" if landed else "the printer never reported the job"
    passed = f"its self-heal bound {bound.name} ({bound.seconds:.0f}s online)" if bound else "its self-heal bound"
    return await _release_claim(
        printer_id,
        item_id,
        cause=cause,
        why=f"the start (action {action_id}) passed {passed}: {situation}",
    )


async def on_action_answered(event: ActionAnswered) -> None:
    """HUB SUBSCRIBER: a ``print_start`` the record answered finally is applied to its unit's claim
    (:func:`apply_answer`). Never raises — a farm-side failure must not break the event chain."""
    if event.command != "print_start":
        return
    try:
        item_id = await printer_action.unit_of(event.action_id)
        if item_id is None:
            return
        await apply_answer(event.printer_id, item_id, Answer(event.answer), event.reason, action_id=event.action_id)
    except Exception:  # noqa: BLE001 — the hub must keep delivering
        logger.exception("dispatch_start: applying start action %s failed", event.action_id)


async def apply_answer(
    printer_id: int, item_id: int, answer: Answer | None, reason: str | None, *, action_id: int
) -> bool:
    """Apply one start answer to unit ``item_id``'s claim (``dispatch_claim.verdict_of``): REACHED keeps the
    claim and drops the dispatch lease; an un-started verdict returns the unit to the queue and drops the
    lease; an outstanding start (``None``) waits; a LATE answer (``dispatch_claim.start_late`` — its bound
    already returned the unit) touches nothing. Idempotent: the return is a conditional transition (a unit
    no longer ``printing`` reads ``moved_on``), and the lease release never refuses. Returns True when the
    unit's row moved back to the queue."""
    if answer is None:
        return False
    verdict = dispatch_claim.verdict_of(answer, late=await _answered_late(item_id, action_id))
    if verdict == "waiting":
        return False
    if verdict == "late":
        logger.warning(
            "dispatch_start: printer %s start (action %s) answered %s after its self-heal bound returned unit %s "
            "— no claim and no lease touched; a print it started runs as FOREIGN (#1150)",
            printer_id,
            action_id,
            answer.value,
            item_id,
        )
        return False
    if verdict == "started":
        plate_occupancy.release_dispatch(printer_id, f"the start was reached (action {action_id})")
        return False
    cause = dispatch_claim.RETURN_CAUSES[verdict]
    return await _release_claim(
        printer_id,
        item_id,
        cause=cause,
        why=f"the start (action {action_id}) was {answer.value}" + (f": {reason}" if reason else ""),
    )


async def _answered_late(item_id: int, action_id: int) -> bool:
    """``dispatch_claim.start_late`` over the record: the start's bound passed, and its unit moved on."""
    from backend.app.core.database import async_session

    async with async_session() as db:
        if not await printer_action.bound_passed_of(db, action_id):
            return False
        status = await db.scalar(select(PrintQueueItem.status).where(PrintQueueItem.id == item_id))
        current = await printer_action.last_start_of(db, item_id)
    return dispatch_claim.start_late(bound_passed=True, unit_status=status, current_start=current, action_id=action_id)


async def _release_claim(printer_id: int, item_id: int, *, cause: str, why: str) -> bool:
    """Return the un-started unit to the queue (``requeue.return_to_queue``, next in line — the run gate
    applied there) and drop the printer's dispatch lease. One WARNING names the evidence. Returns True when
    the row moved (``moved_on`` = somebody else owns it now)."""
    from backend.app.core.database import run_with_retry
    from backend.app.services.requeue import return_to_queue

    async def _do(db):
        outcome = await return_to_queue(db, item_id, cause=cause)  # type: ignore[arg-type]
        await db.commit()
        batch_id = await db.scalar(select(PrintQueueItem.batch_id).where(PrintQueueItem.id == item_id))
        return outcome, batch_id

    outcome, batch_id = await run_with_retry(_do, label=f"dispatch start release item={item_id}")
    plate_occupancy.release_dispatch(printer_id, why)
    if outcome == "moved_on":
        logger.debug("dispatch_start: printer %s unit %s already left 'printing' (%s)", printer_id, item_id, why)
        return False
    logger.warning(
        "dispatch_start: printer %s unit %s never started — %s; returned to the queue (%s, cause=%s)",
        printer_id,
        item_id,
        why,
        outcome,
        cause,
    )
    if outcome in ("returned", "staged"):
        dispatch_progress.emit_queue_item_status(
            item_id=item_id, batch_id=batch_id, printer_id=printer_id, status="pending", phase="assigned"
        )
    return True


async def reevaluate_session(printer_id: int) -> int:
    """SESSION START step (``session_start``, after ``printer_action.reread_session``): decide every
    ``printing`` claim on ``printer_id`` that no event will decide in this process. Returns how many it
    returned to the queue.

    * a start the record still holds OUTSTANDING is left to its event — the answer
      (:func:`on_action_answered`), or its self-heal bound's ``SelfHealDue`` (:func:`on_self_heal_due`; a
      rehydrated start's bound comes from :func:`rehydrated_start_bound`). One whose bound already passed
      before this process could act on it (``bound_passed_at`` on the row: a restart between the pass and
      the return) gets its fallback now;
    * a start the record ANSWERED is applied (:func:`apply_answer`) — a restart between the answer and its
      application must not strand the claim; re-applying one already applied is a no-op;
    * a claim with NO start on the record is judged against this first fresh report
      (``dispatch_claim.judge``): a printer showing no job of ours and no job at all never started it.
    """
    from backend.app.core.database import async_session
    from backend.app.services.print_binding import printers_with_live_print

    released = 0
    async with async_session() as db:
        items = list(
            (
                await db.execute(
                    select(PrintQueueItem)
                    .where(PrintQueueItem.status == "printing")
                    .where(PrintQueueItem.printer_id == printer_id)
                )
            )
            .scalars()
            .all()
        )
        if not items:
            return 0
        starts = {item.id: await printer_action.last_start_of(db, item.id) for item in items}
        archive_printing = printer_id in await printers_with_live_print(db)
    state = printer_manager.get_status(printer_id)
    for item in items:
        action_id = starts[item.id]
        try:
            if action_id is not None:
                if printer_action.outstanding(action_id):
                    if printer_action.bound_passed(action_id):
                        landed = printer_action.reading_of(action_id) is Reading.LANDED
                        # The passed bound's name died with the previous process (``bound_passed_at`` only).
                        released += int(
                            await _take_fallback(printer_id, action_id, item_id=item.id, landed=landed, bound=None)
                        )
                    continue
                answer = await printer_action.settled(action_id)
                released += int(
                    await apply_answer(
                        printer_id, item.id, answer, await printer_action.reason_of(action_id), action_id=action_id
                    )
                )
                continue
            evidence = dispatch_claim.ClaimEvidence(
                live_state=(getattr(state, "state", None) or "").upper(),
                archive_printing=archive_printing,
                dispatch_subtask=(item.dispatch_subtask_id or "").strip(),
                live_subtask=(getattr(state, "subtask_id", None) or "").strip(),
            )
            if dispatch_claim.judge(evidence) == "dead":
                released += int(
                    await _release_claim(
                        printer_id,
                        item.id,
                        cause=dispatch_claim.UNRECORDED_CAUSE,
                        why=f"no start on the record and the session's first report shows no job of ours ({evidence})",
                    )
                )
        except Exception:  # noqa: BLE001 — one claim must not cost the printer's others
            logger.exception("dispatch_start: session re-evaluation failed for printer %s unit %s", printer_id, item.id)
    return released


async def watch_start(
    sent: printer_action.Sent, *, item_id: int, batch_id: int | None, pre_gcode_file: str | None
) -> None:
    """The per-dispatch watch — LIVENESS ONLY, it decides nothing about the claim (the record's answer does,
    :func:`on_action_answered`, and its bound's fallback, :func:`on_self_heal_due`). Two duties:

    * the dispatch-progress phases the queue page shows (``preparing`` / ``printing``), each the first time
      the printer reports it, until the start is answered otherwise or its print runs;
    * the TRANSPORT check, read through the record: when the start's self-heal bound passes
      (:data:`_START_SELF_HEAL`, the record's online clock) with the start still UNACKNOWLEDGED and not landed
      (``Reading.SENT``) on a printer whose ``gcode_file`` never changed (it did not take the file — a forced
      reconnect mid-parse would trigger ``0500_4003``, #1150), the session is half-broken (#887/#936): torn
      down and rebuilt ONCE (the async full teardown, which also drops paho's queued ``project_file`` — the
      #1136 replay). The watch then ends: the unit is the bound's fallback's (:func:`on_self_heal_due`).

    Never raises."""
    printer_id = sent.printer_id
    emitted: set[str] = set()
    try:
        while True:
            answer = await printer_action.settled(sent.action_id)
            status = printer_manager.get_status(printer_id)
            live = getattr(status, "state", None) if status is not None else None
            phase = dispatch_progress.phase_for_observed_state(live)
            if answer in (None, Answer.REACHED) and phase is not None and phase not in emitted:
                emitted.add(phase)
                dispatch_progress.emit_queue_item_status(
                    item_id=item_id, batch_id=batch_id, printer_id=printer_id, status="printing", phase=phase
                )
            # Done once the start is answered and there is no later phase to show: an un-started
            # answer, or a reached start whose printer is past PREPARE (RUNNING emitted ``printing``
            # above; a job word that ended, a PAUSE or a wedged IDLE has nothing more to show).
            if answer is not None and (answer is not Answer.REACHED or live not in ("PREPARE", "SLICING")):
                return
            if answer is None and printer_action.bound_passed(sent.action_id):
                _check_transport(
                    sent, item_id=item_id, gcode_file=getattr(status, "gcode_file", None), pre=pre_gcode_file
                )
                return
            await asyncio.sleep(_WATCH_POLL_S)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — telemetry and transport must never crash the dispatch
        logger.exception("dispatch_start: watch of start action %s failed", sent.action_id)


def _check_transport(sent: printer_action.Sent, *, item_id: int, gcode_file: str | None, pre: str | None) -> None:
    """The transport check at the start's bound (:func:`watch_start`): rebuild a half-broken session when the
    start is unacknowledged and not landed and the printer never took the file."""
    if printer_action.reading_of(sent.action_id) is not Reading.SENT or gcode_file != pre:
        return
    client = printer_manager.get_client(sent.printer_id)
    if client is None:
        return
    logger.warning(
        "dispatch_start: printer %s unit %s — the start (action %s) is unacknowledged at its self-heal bound and "
        "the printer has not taken the file: rebuilding the MQTT session",
        sent.printer_id,
        item_id,
        sent.action_id,
    )
    client.force_reconnect_stale_session(
        f"queue print command (action {sent.action_id}) unacknowledged after "
        f"{_START_SELF_HEAL.seconds:.0f}s online, gcode_file still {pre!r}"
    )
