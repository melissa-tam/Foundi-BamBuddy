"""Maintenance mode: keep the session, keep the print, quiesce the farm's own actions.

THE owner of the operator verb "I am taking this printer" — enter, exit, and the
graceful stand-down in between. Four things live here and nowhere else:

* :func:`enter` — open the durable hold, then quiesce;
* :func:`exit` — close the hold this module owns and wake the scheduler;
* :func:`quiesce` — stop one printer's automatic ACTIONS while its MQTT session, and its
  observation of the printer, stay up. Shared by :func:`enter` and the teardown verb;
* :func:`quiesce_for_teardown` — the same, plus retiring the plate watch, for the one
  caller that is about to DROP the session (a deactivation).

**The hold suspends ACTIONS, not OBSERVATION (2026-09-13).** A hold means "a human owns
this machine": no motion, no eject, no dispatch, no page, no quarantine. It does NOT mean
the farm stops looking — and cooling is air, not motion, so a cooldown that is running
when the hold is entered KEEPS RUNNING: the aux and chamber fans finish their curve, and
when the bed reaches the eject line (or equilibrates) the watch retires the fans and
WITHHOLDS the eject until the hold lifts. Nothing is re-armed on release; the running
watch reads the hold's level on its next tick and releases. Only a session teardown
retires a watch. The cost, stated rather than hidden: a cooldown that ARMS under a hold
is fan-only (the plate hold moves the machine, so it is refused and never re-attempted),
which is slower than a production one.

**NO MODE VERB ENDS A PRINT (2026-09-19 ruling).** Entering maintenance mode used to
call ``print_control.stop_as_operator`` on anything RUNNING/PAUSE/PREPARE/SLICING, and
deactivation inherited that through :func:`quiesce_for_teardown`. It no longer does, and
neither verb may grow it back (``test_code_quality.TestOperatorStopOwnership`` fails CI
on a third caller). The reason is an ownership one: what a hold stands down is the
FARM'S OWN actions — a sweep it commanded, a dispatch it has not yet put on the wire, a
page it would send — and a running print is not one of them. It is the operator's, and
its terminal rides the ordinary lanes (correlation, the plate authority, ``farm_policy``)
exactly as it would have without the hold. An operator who wants the print to end has a
Stop button; a mode switch that also stopped the print gave them no way to take a printer
without losing the plate on it.

**The hold is a ``printer_incident`` row of kind ``service_hold``** (``printer_incidents``
owns the record, this module owns the verb). That is what makes every automation lane's
gate a single read of ``printer_incidents.automation_held`` — the dispatch gate, the AMS
drivers, pause recovery, the tagless reconcile, the hourly nag, the farm-reaction
notifications, and the eject lane's per-tick release permission — with no second flag, no
process dict and no column of our own.

**Why maintenance mode is NOT ``is_active=False``.** ``is_active`` means *this instance
holds an MQTT session for this printer*, and tearing the session down is precisely what
orphaned the automation on 2026-09-12: the eject watchdog could not deliver its stop
(001/009-H2S held a phantom in-flight eject for hours, every ``clear-plate`` answering
409), and ``cooldown_prep.end()`` landed on ``skipped:no_client`` so 010-H2S ran its
cooldown fans for 6.3 h. The order in :func:`quiesce_for_teardown` is the lesson: every
actuator is retired while the wire is still there.

**Quiesce is a best-effort SEQUENCE, not a transaction.** Each step is wrapped, because
the one thing worse than a step failing is a step failing and skipping the ones after
it — the sweep, the job and the lease are independent, and a human is standing in front
of the machine. The durable row goes FIRST in :func:`enter` for the same reason:
whatever the quiesce manages, the printer reads held, so nothing automatic comes back on
the next tick.
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
from backend.app.services.plate_occupancy import plate_occupancy
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

#: How long :func:`quiesce_for_teardown` waits for a cancelled plate watch to run its
#: ``finally`` — the plate hold's release and both fans-off publishes. Generous on
#: purpose: it bounds a task that should finish in milliseconds (the publishes are
#: fire-and-forget MQTT), and the cost of being wrong is the 6.3-hour fan run this whole
#: module exists for, against at most ten seconds on the operator's click.
_STAND_DOWN_WAIT_S = 10.0


@dataclass(frozen=True)
class QuiesceReport:
    """What the quiesce actually had to do — one bool per action it took.

    The operator's toast reads these back ("the sweep was stopped"), so each one means
    *this call changed that*, never *that was in some state*: a second enter on an
    already-quiet printer reports two Falses, which is the honest answer.

    Two fields are deliberately absent, for the same reason in two directions: a
    cooldown bool, because entering a hold no longer ENDS a cooldown (the fans finish
    their curve and the eject is withheld); and a job bool, because entering a hold no
    longer stops a PRINT. Neither could carry anything but False, and a field that is
    always False is a claim the UI would go on making.
    """

    eject_stopped: bool = False
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
    eject_stopped: bool = False
    lease_revoked: bool = False


async def quiesce(printer_id: int, *, cause: str) -> QuiesceReport:
    """Stop ``printer_id``'s automatic ACTIONS, session, watch and PRINT intact. NEVER raises.

    Every step here is something the FARM did and can take back. That is the whole
    selection rule, and it is what keeps the list short:

    1. **an in-flight eject sweep** — a job the farm itself commanded, re-driven through
       the ONE kill path (``eject.remote.redrive_eject_stop``, stage ``service_hold``).
       A sweep is never resumed; the stopped job's terminal resolves the eject
       ``unverified`` and the plate gate stays human-clear.
    2. **the dispatch lease** — revoked, so a dispatch already past its decision point
       is refused ``lease_revoked`` at commit and the scheduler unwinds the row rather
       than printing onto a printer somebody has taken.

    **A RUNNING PRINT IS NOT ONE OF THEM (2026-09-19).** Step 2 used to be
    ``print_control.stop_as_operator`` over ``ACTIVE_PRINT_STATES``. It is gone, and
    with it the last mode verb that ended a print: the print belongs to the operator,
    not to the farm, and its terminal rides the ordinary lanes under a hold exactly as
    it would without one (FINISH → completed, the plate gated, the eject withheld until
    the hold lifts). A print that FAULTS while held gets no farm recovery — every
    automatic lane keeps standing down — which is the ruling, not an oversight.

    **The armed plate watch is deliberately LEFT RUNNING (2026-09-13).** Standing it down
    used to be step 1, and it retired the cooldown with it — both fans commanded off over
    a bed that was still hot, because the watch's ``finally`` cannot tell "the session is
    going" from "a human is at the machine". A hold stops actions, and moving air is not
    an action: the watch keeps polling, the fans finish their curve, and the EJECT is
    withheld at the release boundary until the hold lifts
    (``watch_bed_and_clear(held=…)``). The caller that genuinely needs the watch retired —
    the one about to drop the MQTT session — asks for it by name:
    :func:`quiesce_for_teardown`.
    """
    eject_stopped = False
    lease_revoked = False

    try:
        if eject_cooldown_monitor.active_watch(printer_id) is not None:
            # One INFO line so a triage log over ``[service-hold]`` says what happened to
            # the plate: nothing was cancelled, and the fans an operator can hear are
            # running on purpose.
            logger.info(
                "[service-hold] printer %s: cooldown watch continues under the hold (fans only, eject withheld)",
                printer_id,
            )
    except Exception:  # noqa: BLE001 — a log line is never worth the steps below
        logger.exception("[service-hold] printer %s: reading the plate watch failed (%s)", printer_id, cause)

    try:
        if plate_occupancy.eject_identity(printer_id) is not None:
            eject_stopped = await eject_remote.redrive_eject_stop(printer_id, stage="service_hold")
    except Exception:  # noqa: BLE001
        logger.exception("[service-hold] printer %s: stopping the in-flight eject failed (%s)", printer_id, cause)

    try:
        lease_revoked = plate_occupancy.revoke_lease(printer_id, cause)
    except Exception:  # noqa: BLE001
        logger.exception("[service-hold] printer %s: revoking the dispatch lease failed (%s)", printer_id, cause)

    logger.info(
        "[service-hold] printer %s quiesced (%s): eject_stopped=%s lease_revoked=%s (any running print is left alone)",
        printer_id,
        cause,
        eject_stopped,
        lease_revoked,
    )
    return QuiesceReport(eject_stopped=eject_stopped, lease_revoked=lease_revoked)


async def quiesce_for_teardown(printer_id: int, *, cause: str) -> QuiesceReport:
    """Quiesce ``printer_id`` AND retire its plate watch. The verb for "the session goes".

    The ONE sequence for a caller that is about to drop the MQTT session — today the
    deactivate branch of ``PATCH /printers/{id}``, tomorrow ``POST /printers/{id}/disconnect``
    and ``DELETE /printers/{id}``, which orphan the automation today and should inherit
    this order rather than re-derive it.

    It is the plate watch PLUS :func:`quiesce`, so it inherits that function's rule
    too: **deactivating a printer does not end its print either.** The printer keeps
    printing from its own USB storage with nobody watching; the queue row stays
    ``printing`` until the reconcile that runs on re-activation resolves it.

    The watch goes FIRST and is AWAITED, bounded. ``stand_down``'s cancellation runs the
    watch task's own ``finally``, which retires the :mod:`cooldown_prep` — plate hold
    released, both cooldown fans commanded OFF — and every one of those publishes needs a
    live client: 2026-09-12, 010-H2S ran its cooldown fans 22 821 s because the session
    went first and ``prep.end()`` landed on ``skipped:no_client``. ``cancel()`` only
    SCHEDULES the CancelledError, so the ``finally`` runs on a LATER loop turn while the
    caller deletes the client the instant this returns — the 2026-09-12 probe caught
    exactly that one-millisecond gap on the deactivate path. The wait is non-raising
    (``asyncio.wait`` collects the cancellation instead of re-raising it), so a watch that
    will not retire costs a WARNING and the quiesce still runs.

    The plate's STORED policy is untouched: re-activating the printer calls
    ``eject_cooldown_monitor.reconsider``, which re-arms exactly what was running.
    """
    try:
        retiring = eject_cooldown_monitor.stand_down(printer_id, cause)
        if retiring is not None:
            _done, pending = await asyncio.wait({retiring}, timeout=_STAND_DOWN_WAIT_S)
            if pending:
                logger.warning(
                    "[service-hold] printer %s: the plate watch did not retire within %.0f s (%s) — "
                    "its cooldown fans may still be running",
                    printer_id,
                    _STAND_DOWN_WAIT_S,
                    cause,
                )
            else:
                logger.info(
                    "[service-hold] printer %s: plate watch retired before the session drops (%s)", printer_id, cause
                )
    except Exception:  # noqa: BLE001 — the quiesce below is independent of this step
        logger.exception("[service-hold] printer %s: standing the plate watch down failed (%s)", printer_id, cause)

    return await quiesce(printer_id, cause=cause)


async def enter(db: AsyncSession, printer_id: int, *, actor: str) -> HoldVerdict:
    """Put ``printer_id`` in maintenance mode. Idempotent.

    Durable FIRST: the row is what every lane's gate reads, so it is opened before a
    single actuator is touched — a quiesce that dies half-way still leaves a printer
    that nothing automatic will act on.

    An operator re-entering a hold that already stands gets ``already_held=True`` **and
    a fresh quiesce**: the second click means "make this machine quiet", and the reason
    it is being clicked again is usually that something came back (a sweep, a lease)
    which the first entry never saw.

    What it does NOT stop is the PRINT: a running job — farm or foreign — keeps printing
    and reaches its own terminal through the ordinary lanes. Nor the cooling: an armed
    plate watch keeps running (fans only, its eject withheld until the release), because
    a hot bed has to cool whoever owns the printer. The plate stays raised where the hold
    left it, and the eject's own first Z move takes it from there whenever it finally
    runs.

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
        eject_stopped=report.eject_stopped,
        lease_revoked=report.lease_revoked,
    )


async def exit(db: AsyncSession, printer_id: int, *, actor: str) -> bool:
    """Take ``printer_id`` out of maintenance mode. True iff THIS call released a hold.

    Closes the row this module owns by ID — never ``close_open_for_printer``, which
    would also close a jam or a lost-Z frame standing beside the hold, i.e. the faults
    the operator went in to look at.

    **Nothing is re-armed here (2026-09-13).** The plate watch was never stood down, and
    it reads the hold as a per-tick LEVEL — so a plate that finished cooling under the hold
    dispatches its eject on the watch's very next poll, and one that is still cooling
    simply carries on. The only nudge left is one dispatch kick, which lets the scheduler
    re-read a printer whose hold no longer blocks it instead of waiting for the fallback
    tick. It is level-triggered too, so releasing a hold that changed nothing is a no-op
    rather than a second decision.
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
