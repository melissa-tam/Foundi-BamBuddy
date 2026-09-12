"""Maintenance mode: keep the session, quiesce the automation.

THE owner of the operator verb "I am taking this printer" — enter, exit, and the
graceful stand-down in between. Three things live here and nowhere else:

* :func:`enter` — open the durable hold, then quiesce;
* :func:`exit` — close the hold this module owns, then let the automation re-decide;
* :func:`quiesce` — take one printer's automation down WHILE its MQTT session is still
  up, shared by :func:`enter` and the deactivate branch of ``PATCH /printers/{id}``.

**The hold is a ``printer_incident`` row of kind ``service_hold``** (``printer_incidents``
owns the record, this module owns the verb). That is what makes every automation lane's
gate a single read of ``printer_incidents.automation_held`` — the dispatch gate, the
plate-policy driver, the AMS drivers, pause recovery, the tagless reconcile, the hourly
nag and the farm-reaction notifications — with no second flag, no process dict and no
column of our own.

**Why maintenance mode is NOT ``is_active=False``.** ``is_active`` means *this instance
holds an MQTT session for this printer*, and tearing the session down is precisely what
orphaned the automation on 2026-09-12: the eject watchdog could not deliver its stop
(001/009-H2S held a phantom in-flight eject for hours, every ``clear-plate`` answering
409), and ``cooldown_prep.end()`` landed on ``skipped:no_client`` so 010-H2S ran its
cooldown fans for 6.3 h. The order in :func:`quiesce` is the lesson: every actuator is
retired while the wire is still there.

**Quiesce is a best-effort SEQUENCE, not a transaction.** Each step is wrapped, because
the one thing worse than a step failing is a step failing and skipping the three after
it — the fans, the sweep, the job and the lease are independent, and a human is standing
in front of the machine. The durable row goes FIRST in :func:`enter` for the same
reason: whatever the quiesce manages, the printer reads held, so nothing automatic
comes back on the next tick.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from backend.app.models.printer_incident import (
    KIND_SERVICE_HOLD,
    RESOLVE_OPERATOR,
    STATUS_RESOLVED,
)
from backend.app.services import printer_incidents
from backend.app.services.eject import remote as eject_remote
from backend.app.services.eject.monitor import eject_cooldown_monitor

# ACTIVE_PRINT_STATES lives with the occupancy domain, which owns "what counts as an
# active job" (``print_scheduler`` re-exports the same object). Imported from the origin
# so the quiesce cannot come to disagree with the plate authority about PAUSE — a paused
# job IS a job to stop before hands go in.
from backend.app.services.plate_occupancy import ACTIVE_PRINT_STATES, plate_occupancy
from backend.app.services.print_control import stop_as_operator
from backend.app.services.printer_manager import printer_manager

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: The cause string the enter path stamps on every quiesce step's log line. The
#: deactivate branch passes its own (``"deactivate"``), so one grep over
#: ``[service-hold]`` tells the two apart in a triage log.
CAUSE_ENTER = "service hold"

#: Why the scheduler is woken when a hold lifts. Same vocabulary as the other kick
#: producers (``enqueue`` / ``manual_start`` / ``dry_run_enqueue``).
_KICK_REASON = "service_hold_released"

#: How long :func:`quiesce` waits for a cancelled plate watch to run its ``finally`` —
#: the plate hold's release and both fans-off publishes. Generous on purpose: it bounds
#: a task that should finish in milliseconds (the publishes are fire-and-forget MQTT),
#: and the cost of being wrong is the 6.3-hour fan run this whole module exists for,
#: against at most ten seconds on the operator's click.
_STAND_DOWN_WAIT_S = 10.0


@dataclass(frozen=True)
class QuiesceReport:
    """What the quiesce actually had to do — one bool per actuator it retires.

    The operator's toast reads these back ("the print was stopped, the sweep was
    stopped"), so each one means *this call changed that*, never *that was in some
    state*: a second enter on an already-quiet printer reports four Falses, which is
    the honest answer.
    """

    cooldown_ended: bool = False
    eject_stopped: bool = False
    job_stopped: bool = False
    lease_revoked: bool = False


@dataclass(frozen=True)
class HoldVerdict:
    """:func:`enter`'s answer: the hold's state, plus what the quiesce did.

    ``held`` is True whenever the printer is held when this call returns — which is
    always, since the hold is opened first and an existing one is the same outcome.
    ``already_held`` is what tells an operator their click was the second one.
    """

    held: bool
    already_held: bool
    cooldown_ended: bool = False
    eject_stopped: bool = False
    job_stopped: bool = False
    lease_revoked: bool = False


async def quiesce(printer_id: int, *, cause: str) -> QuiesceReport:
    """Take ``printer_id``'s automation down gracefully. NEVER raises.

    In order, and each step guarded so one failure cannot skip the rest:

    1. **the armed plate watch** — ``stand_down`` cancels it, and the watch task's own
       ``finally`` retires the :mod:`cooldown_prep`: plate hold released, both cooldown
       fans commanded OFF. It has to happen HERE, before any session teardown, because
       that ``finally`` needs a live client to publish ``M106 P2 S0`` / ``M106 P3 S0``
       (2026-09-12, 010-H2S: 22 821 s of fans) — and the cancelled task is then AWAITED,
       bounded, because a cancel is only SCHEDULED and the deactivate caller deletes the
       client the moment this returns. The plate's STORED policy is untouched, so
       :func:`exit`'s ``reconsider`` re-arms exactly what was running.
    2. **an in-flight eject sweep** — re-driven through the ONE kill path
       (``eject.remote.redrive_eject_stop``, stage ``service_hold``). A sweep is never
       resumed; the stopped job's terminal resolves the eject ``unverified`` and the
       plate gate stays human-clear.
    3. **a running or paused job** (farm or foreign) — the operator stop, so the unit
       lands ``cancelled`` with a ``stop_source`` and the run holds for RESUME instead
       of counting a failure.
    4. **the dispatch lease** — revoked, so a dispatch already past its decision point
       is refused ``lease_revoked`` at commit and the scheduler unwinds the row rather
       than printing onto a printer somebody has taken.
    """
    cooldown_ended = False
    eject_stopped = False
    job_stopped = False
    lease_revoked = False

    try:
        # Read BEFORE the cancel: ``stand_down`` is a no-op on a printer with no watch,
        # and the operator's toast must not claim a cooldown was ended on a printer
        # whose plate nothing was watching.
        cooldown_ended = eject_cooldown_monitor.active_watch(printer_id) is not None
        retiring = eject_cooldown_monitor.stand_down(printer_id, cause)
        if retiring is not None:
            # AWAIT the cancelled watch before going on. ``cancel()`` only SCHEDULES the
            # CancelledError, so the ``finally`` that publishes the fans off runs on a
            # LATER loop turn — and the deactivate caller deletes the MQTT client the
            # instant this function returns. 2026-09-12 probe: the quiesce logged
            # ``cooldown_ended=True`` at 07:07:08,545 and the prep logged
            # ``off=skipped:no_client`` at 07:07:08,546. Bounded and non-raising
            # (``asyncio.wait`` collects the cancellation instead of re-raising it), so a
            # watch that will not retire costs a WARNING and the three steps below, never
            # the quiesce.
            _done, pending = await asyncio.wait({retiring}, timeout=_STAND_DOWN_WAIT_S)
            if pending:
                logger.warning(
                    "[service-hold] printer %s: the plate watch did not retire within %.0f s (%s) — "
                    "its cooldown fans may still be running",
                    printer_id,
                    _STAND_DOWN_WAIT_S,
                    cause,
                )
    except Exception:  # noqa: BLE001 — the three steps below are independent of this one
        logger.exception("[service-hold] printer %s: standing the plate watch down failed (%s)", printer_id, cause)

    try:
        if plate_occupancy.eject_identity(printer_id) is not None:
            eject_stopped = await eject_remote.redrive_eject_stop(printer_id, stage="service_hold")
    except Exception:  # noqa: BLE001
        logger.exception("[service-hold] printer %s: stopping the in-flight eject failed (%s)", printer_id, cause)

    try:
        state = printer_manager.get_status(printer_id)
        live = (getattr(state, "state", None) or "").upper() if state is not None else ""
        if live in ACTIVE_PRINT_STATES:
            job_stopped = stop_as_operator(printer_id)
    except Exception:  # noqa: BLE001
        logger.exception("[service-hold] printer %s: stopping the running job failed (%s)", printer_id, cause)

    try:
        lease_revoked = plate_occupancy.revoke_lease(printer_id, cause)
    except Exception:  # noqa: BLE001
        logger.exception("[service-hold] printer %s: revoking the dispatch lease failed (%s)", printer_id, cause)

    logger.info(
        "[service-hold] printer %s quiesced (%s): cooldown_ended=%s eject_stopped=%s job_stopped=%s lease_revoked=%s",
        printer_id,
        cause,
        cooldown_ended,
        eject_stopped,
        job_stopped,
        lease_revoked,
    )
    return QuiesceReport(
        cooldown_ended=cooldown_ended,
        eject_stopped=eject_stopped,
        job_stopped=job_stopped,
        lease_revoked=lease_revoked,
    )


async def enter(db: AsyncSession, printer_id: int, *, actor: str) -> HoldVerdict:
    """Put ``printer_id`` in maintenance mode. Idempotent.

    Durable FIRST: the row is what every lane's gate reads, so it is opened before a
    single actuator is touched — a quiesce that dies half-way still leaves a printer
    that nothing automatic will act on.

    An operator re-entering a hold that already stands gets ``already_held=True`` **and
    a fresh quiesce**: the second click means "make this machine quiet", and the reason
    it is being clicked again is usually that something came back (a screen-started
    print, a sweep, a lease) which the first entry never saw.

    Works on a DEACTIVATED printer too (``is_active=False``): there is nothing to
    quiesce with no session, and the hold is still worth recording — it is what stops
    the kick-driven scheduler dispatching within ~1 s of re-activation.
    """
    row = await printer_incidents.open_declared(db, printer_id, kind=KIND_SERVICE_HOLD)
    already_held = row is None
    report = await quiesce(printer_id, cause=CAUSE_ENTER)
    await _broadcast(printer_id)
    logger.info(
        "[service-hold] printer %s ENTERED maintenance mode by %s (already_held=%s, %s)",
        printer_id,
        actor,
        already_held,
        report,
    )
    return HoldVerdict(
        held=True,
        already_held=already_held,
        cooldown_ended=report.cooldown_ended,
        eject_stopped=report.eject_stopped,
        job_stopped=report.job_stopped,
        lease_revoked=report.lease_revoked,
    )


async def exit(db: AsyncSession, printer_id: int, *, actor: str) -> bool:
    """Take ``printer_id`` out of maintenance mode. True iff THIS call released a hold.

    Closes the row this module owns by ID — never ``close_open_for_printer``, which
    would also close a jam or a lost-Z frame standing beside the hold, i.e. the faults
    the operator went in to look at.

    Then the automation is asked to decide again, rather than told what to do:
    ``reconsider`` re-runs the plate-policy driver on the current view (a plate still
    gated under ``CooldownEject`` resumes its cooldown and auto-ejects at threshold),
    and one dispatch kick lets the scheduler re-read a printer whose hold no longer
    blocks it. Both are level-triggered, so releasing a hold that changed nothing is a
    no-op rather than a second decision.
    """
    row = await printer_incidents.get_open(db, printer_id, kinds={KIND_SERVICE_HOLD})
    if row is None:
        logger.info("[service-hold] printer %s is not in maintenance mode — nothing to release (%s)", printer_id, actor)
        return False
    closed = await printer_incidents.close(db, row.id, status=STATUS_RESOLVED, source=RESOLVE_OPERATOR)
    if closed is None:
        # Somebody else closed it between the read and the close. They own the
        # re-arm; re-running it here would be a second decision on one release.
        logger.info("[service-hold] printer %s hold %s closed underneath this release", printer_id, row.id)
        return False

    eject_cooldown_monitor.reconsider(printer_id, "service hold released")
    _kick_dispatch(printer_id)
    await _broadcast(printer_id)
    logger.info("[service-hold] printer %s LEFT maintenance mode (released by %s)", printer_id, actor)
    return True


def _kick_dispatch(printer_id: int) -> None:
    """Wake the scheduler for this printer. Lazy import: ``dispatch_kick`` is a leaf,
    and every producer reaches it the same way."""
    from backend.app.services.dispatch_kick import dispatch_kick

    dispatch_kick.kick(_KICK_REASON, printer_id)


async def _broadcast(printer_id: int) -> None:
    """Push one ``printer_status`` frame so open cards pick the hold up immediately.

    The same helper the quarantine flip and the plate authority use
    (``printer_manager._broadcast_status_change``): the MQTT-driven broadcast dedupes on
    a status key that deliberately excludes Bambuddy-side flags, so a flag the printer
    never pushes needs its own emit. It no-ops with no live state and swallows its own
    transport errors — a hold must not fail because a socket did.
    """
    await printer_manager._broadcast_status_change(printer_id)
