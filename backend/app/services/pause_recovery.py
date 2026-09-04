"""The pause-recovery lane: why is this printer PAUSEd, and may the farm answer it?

Two causes live here, and the split of duties is the same for both.

**What this module owns.** DETECTION (a per-push wire sampler), the HOLD RECORD
(a ``printer_incident`` row — never a process dict) and the DECISION: *answer the
prompt*, *stop this print*, or *stand aside*. Nothing else. Everything that happens
AFTER a terminal — the unit's disposition, the requeue, the plate gate, the held-bed
lift, the second-trip read — belongs to ``farm_policy.on_terminal``, which already
owns run/retry/quarantine and the one post-terminal motion. This lane never awaits a
terminal it does not own and never imports ``main``.

``power_loss`` (2026-09-04 fleet outage)
    A power cut rebooted every printer while the server stayed up on its UPS. Each
    came back connected, ``PAUSE``, offering the firmware's own recovery prompt
    ``0300_8007`` — *"There was an unfinished print job when the printer lost power…
    you can try resuming"* — and sat there ~5 h until a human touched its screen. The
    vendored action catalog lists exactly ``RESUME_PRINTING`` / ``STOP_PRINTING`` for
    that code, and printer 8 proved the resume takes (RUNNING one second later). So
    the farm answers it: resume, confirm on the wire, and HOLD for a human only when
    the answer did not take.

``plate_vision`` (operator requirement 2026-09-04)
    The printer's own pre-print vision check trips (``_HMS_PLATE_OCCUPANCY_CODES``)
    and the firmware PAUSEs the job at layer 0. The farm used to raise a human-clear
    gate and leave the unit ``printing`` forever — 13 trips produced 22 operator stops
    and 0 requeues. Now the lane STOPS the print so a terminal exists, having first
    stamped the durable farm-abort mark on the row; the disposition (re-check requeue
    vs confirmed hold) is decided at that terminal by ``farm_policy``.

Restart durability (F7)
    The only durable state is the incident row. Three things live in process memory
    and each names how the wire re-answers it:

    * ``_in_flight`` — one driver handle per printer, so a standing prompt spawns one
      recovery rather than one per push. A restart drops it; the very next push
      re-derives "the prompt is standing" for free and spawns again. "Already decided"
      is the incident row, which survives, so the cost of a restart is at most ONE
      idempotent resume.
    * ``_seen`` — the per-printer wire sample (``connection_epoch`` + the measured
      outage) behind the reconnect EDGE. A restart re-seeds without edging, exactly
      like ``spool_recovery._wire_sample`` and ``hms_edges``: an outage that ended
      before we looked is not an edge we witnessed, and the measured duration is then
      simply unknown (the notification drops the sentence rather than inventing one).
    * ``_summary`` — the fleet accumulator for ONE summary page per outage. A restart
      mid-window loses the counts, which are the whole point of the page; that is
      accepted (F9) and paid for by the log line every window close emits whether or
      not the page fires, so a suppressed page and a dead lane are never confused.

Every entry point here is fire-and-forget and fully guarded (invariant 10): no
farm-side failure may crash the MQTT status flow.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from backend.app.models.printer_incident import (
    KIND_PLATE_VISION,
    KIND_POWER_LOSS,
    KIND_Z_REFERENCE_LOST,
    RESOLVE_OPERATOR,
    STATUS_ESCALATED,
    STATUS_RECOVERING,
    STATUS_RESOLVED,
)
from backend.app.services import printer_incidents
from backend.app.services.hms_errors import (
    POWER_LOSS_PROMPT_CODES,
    POWER_LOSS_RESUME_FAILED_CODES,
    power_loss_hold_active,
    power_loss_prompt_standing,
    power_loss_resume_failed,
)
from backend.app.services.plate_occupancy import ACTIVE_PRINT_STATES, plate_occupancy
from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)


# --- latency budget (rows in the AMS skill's table; every number carries its why) ---

# Settle before the lane looks at the wire it is about to act on. MIRRORS
# ``spool_recovery._RUNOUT_RESUME_SETTLE_S`` and is UNMEASURED for this purpose: the
# only figure the 2026-09-04 outage produced — printer 8 resumed "+14 s" after its
# reconnect — is that very constant firing in the refill lane, not a measurement of
# firmware readiness. It also rides out the post-boot SECOND reconnect observed on
# printers 1 and 3 (09:28:58 / 09:29:00, ~60 s after the first), which would otherwise
# cancel a resume mid-flight.
_POWER_LOSS_SETTLE_S = 15.0
# A ``resume_print`` that returns False means the send did not go out — the session is
# mid-churn (the second reconnect above) rather than the firmware refusing. ONE retry,
# far enough out to clear that churn, then the lane holds.
_POWER_LOSS_RETRY_S = 30.0
# How long the wire gets to show RUNNING after an accepted resume. Same figure and same
# reasoning as ``spool_recovery._RUNOUT_RESUME_CONFIRM_S``: an ACK proves acceptance,
# never execution, so every command is confirmed against the wire.
_POWER_LOSS_CONFIRM_S = 30.0
# Poll step of that confirm wait. The caller's budget, not ``await_state``'s.
_POWER_LOSS_POLL_S = 1.0
# The fleet summary's accumulation window, from the FIRST decision. Comfortably longer
# than the 26 s the 2026-09-04 disconnect burst spanned plus the settle above, so one
# outage produces one page.
_SUMMARY_WINDOW_S = 120.0
# How many printers must lose their session together before the farm calls it an
# OUTAGE rather than a printer. Derived from what the discriminator has to survive: a
# stale-reconnect, the maintenance toggle and the 2026-08-25 mark-plate-occupied flow
# each drop ONE session, and a two-printer coincidence is a plausible network blip on a
# shared switch. Three is the smallest count that cannot be any of those.
_OUTAGE_BURST_MIN_PRINTERS = 3
# ...within this window of each other. The 2026-09-04 burst spanned 26 s
# (09:25:37-09:26:03); 120 s is ~4.6x that, which covers a slower rolling brown-out
# without reaching across two unrelated single-printer events.
_OUTAGE_BURST_WINDOW_S = 120.0
# A ``stop_print`` that returns False on a vision trip is the same session-churn shape
# as the resume above, but the printer is HERE and connected (it just paused itself),
# so the retry is short.
_VISION_STOP_RETRY_S = 5.0


# --- process memory (see the module docstring's restart-durability story) -----------


@dataclass(frozen=True)
class _WireSample:
    """What the sampler remembers per printer, to derive a RECONNECT edge from."""

    epoch: int
    # Wall-clock seconds the last observed outage lasted, or None when this process
    # never saw its disconnect edge (a restart mid-outage, or a first-ever connect).
    outage_s: float | None


@dataclass
class _OutageSummary:
    """One accumulator per outage window. Printer ids, not counts, so a printer that
    is decided twice inside the window cannot inflate the page."""

    opened_at: float
    resumed: set[int] = field(default_factory=set)
    held: set[int] = field(default_factory=set)
    stopped_ejects: set[int] = field(default_factory=set)
    held_by_fault: set[int] = field(default_factory=set)
    outage_s: float | None = None


_in_flight: dict[int, asyncio.Task] = {}
_seen: dict[int, _WireSample] = {}
_summary: _OutageSummary | None = None
_summary_task: asyncio.Task | None = None


def _reset_state() -> None:
    """Test hook: drop every piece of process memory between cases."""
    global _summary, _summary_task
    for task in list(_in_flight.values()):
        task.cancel()
    _in_flight.clear()
    _seen.clear()
    if _summary_task is not None:
        _summary_task.cancel()
    _summary_task = None
    _summary = None


# --- entry point 1: the per-push sampler -------------------------------------------


def note_status_push(printer_id: int, state) -> None:
    """Per-push wire sampler. Sync, DB-free, in-memory — and it NEVER raises.

    Rides the ~1 Hz status push beside ``spool_recovery.note_demand_watch``. It does
    exactly two things:

    * spawns ONE power-loss recovery driver while the prompt stands
      (:func:`hms_errors.power_loss_hold_active` = live ``PAUSE`` and ``0300_8007``
      standing), deduped by :data:`_in_flight` so a prompt that stands for minutes
      still gets a single driver;
    * derives the RECONNECT edge (``connection_epoch`` advanced) and, when the fleet's
      disconnect anchors say this was an OUTAGE rather than one printer, arms the
      lost-Z-reference hold for a printer that came back with a part on its plate.

    Nothing else lives here. The PAUSE->RUNNING edge that ENDS a power-loss hold is
    already ``spool_recovery.on_observed_running``'s, spawned from the sampler beside
    this one: the incident machine's close paths are kind-agnostic, so a hold this lane
    opened is closed by the operator's screen resume with no code of its own.
    """
    try:
        epoch = int(getattr(state, "connection_epoch", 0) or 0)
        anchor = getattr(state, "disconnected_at", None)
        prev = _seen.get(printer_id)

        outage_s = prev.outage_s if prev is not None else None
        reconnected = prev is not None and epoch > prev.epoch
        if reconnected and anchor is not None:
            # The session we just lost ended NOW; ``disconnected_at`` is kept across the
            # reconnect precisely so this subtraction is possible.
            outage_s = max(0.0, time.time() - float(anchor))
        _seen[printer_id] = _WireSample(epoch=epoch, outage_s=outage_s)

        if reconnected:
            _maybe_arm_z_reference_hold(printer_id, anchor)

        if not power_loss_hold_active(state):
            return
        task = _in_flight.get(printer_id)
        if task is not None and not task.done():
            return
        subtask = (getattr(state, "subtask_id", None) or "").strip() or None

        from backend.app.core.tasks import spawn_background_task

        logger.info(
            "[pause-recovery] printer %s is at the power-loss prompt (job %s) — recovery driver spawned",
            printer_id,
            subtask or "-",
        )
        _in_flight[printer_id] = spawn_background_task(
            _recover_power_loss(printer_id, subtask),
            name=f"power-loss-recover-p{printer_id}",
        )
    except Exception:  # noqa: BLE001 — invariant 10: never crash the status flow
        logger.exception("[pause-recovery] status sampler failed for printer %s", printer_id)


# --- the power-loss driver ----------------------------------------------------------


async def _recover_power_loss(printer_id: int, observed_subtask: str | None) -> None:
    """Decide what the farm may do about the power-loss prompt on ``printer_id``.

    ``observed_subtask`` is the job id the sampler saw AT the trip; the decision is
    relative to it, which is the only way "the printer moved on" can be told from "the
    printer is still holding the job we saw".

    Branches, IN ORDER, one log line each — the order is the contract:

    (a) STAND DOWN — disconnected / no longer PAUSE / the prompt is gone / the job
        changed. The operator beat us, or this is not the state we decided on.
    (b) INTERRUPTED EJECT — the printer holds a pending sweep. A sweep is NEVER
        resumed (the 2026-07-31 gouged-plate class): the eject lane re-drives its OWN
        kill. And unless the model's Z re-reference has been laddered, the printer also
        earns the lost-Z hold — it rebooted mid-sweep WITH the part on the plate, so
        its Z frame is fiction and the operator's later "Eject plate" must be refused
        until that part comes off by hand.
    (c) OPEN INCIDENT — another lane already owns this printer; open nothing, stand
        aside (F7). For a runout hold this is not a gap: the refill lane resumes on
        PRESENCE evidence, and that resume answers the power-loss prompt too (printer 8
        proved it on 2026-09-04).
    (d) RESUME — farm unit or FOREIGN print alike (operator ruling: parity). Success is
        a LOG LINE and a summary entry, with no durable record; failure is a HOLD.
    """
    try:
        await asyncio.sleep(_POWER_LOSS_SETTLE_S)
        state = printer_manager.get_status(printer_id)
        stand_down = _stand_down_reason(state, observed_subtask)
        if stand_down is not None:
            logger.info(
                "[pause-recovery] printer %s power-loss recovery stood down after the settle — %s",
                printer_id,
                stand_down,
            )
            return

        if plate_occupancy.eject_identity(printer_id) is not None:
            await _stop_interrupted_eject(printer_id)
            return

        from backend.app.core.database import async_session

        async with async_session() as db:
            incident = await printer_incidents.get_open(db, printer_id)
        if incident is not None:
            logger.info(
                "[pause-recovery] printer %s held by an open %s incident (%s) — standing aside, "
                "the prompt is that lane's to answer",
                printer_id,
                incident.kind,
                incident.status,
            )
            _record(printer_id, "held_by_fault")
            return

        await _resume_after_power_loss(printer_id, observed_subtask)
    except Exception:  # noqa: BLE001 — an assist lane must never crash its caller
        logger.exception("[pause-recovery] power-loss recovery failed for printer %s", printer_id)
    finally:
        _in_flight.pop(printer_id, None)


def _stand_down_reason(state, observed_subtask: str | None) -> str | None:
    """Why branch (a) applies, or None when the decision still stands."""
    if state is None or not getattr(state, "connected", False):
        return "printer is not connected"
    live = (getattr(state, "state", None) or "").upper()
    if live != "PAUSE":
        return f"live state is {live or 'unknown'}, not PAUSE (the operator resumed or stopped it)"
    if not power_loss_prompt_standing(getattr(state, "hms_errors", None) or []):
        return "the power-loss prompt is no longer standing"
    live_subtask = (getattr(state, "subtask_id", None) or "").strip() or None
    if live_subtask != observed_subtask:
        return f"the job changed ({observed_subtask or '-'} -> {live_subtask or '-'})"
    return None


async def _stop_interrupted_eject(printer_id: int) -> None:
    """Branch (b): kill the sweep the outage interrupted, and hold the plate for hands."""
    from backend.app.services.eject.remote import redrive_eject_stop

    stopped = await redrive_eject_stop(printer_id, stage="power_loss")
    logger.warning(
        "[pause-recovery] printer %s rebooted mid-EJECT — the sweep is stopped, never resumed (re-drive %s)",
        printer_id,
        "sent" if stopped else "found nothing to stop",
    )
    _record(printer_id, "stopped_ejects")
    await _open_z_reference_hold(printer_id, cause="rebooted mid-eject with the part still on the plate")


async def _resume_after_power_loss(printer_id: int, observed_subtask: str | None) -> None:
    """Branch (d): answer the prompt, confirm it took, or hold for a human."""
    client = printer_manager.get_client(printer_id)
    if client is None:
        await _hold_power_loss(printer_id, observed_subtask, reason="resume_refused")
        return

    if not client.resume_print():
        logger.info(
            "[pause-recovery] printer %s resume_print send returned False (session mid-churn?) — one retry in %.0fs",
            printer_id,
            _POWER_LOSS_RETRY_S,
        )
        await asyncio.sleep(_POWER_LOSS_RETRY_S)
        stand_down = _stand_down_reason(printer_manager.get_status(printer_id), observed_subtask)
        if stand_down is not None:
            logger.info("[pause-recovery] printer %s power-loss resume retry stood down — %s", printer_id, stand_down)
            return
        if not client.resume_print():
            await _hold_power_loss(printer_id, observed_subtask, reason="resume_refused")
            return

    if not await _confirm_running(printer_id):
        await _hold_power_loss(printer_id, observed_subtask, reason="resume_failed")
        return

    outage = _outage_minutes(printer_id)
    logger.info(
        "[pause-recovery] printer %s RESUMED after power loss (outage %s, job %s, origin %s)",
        printer_id,
        f"~{outage} min" if outage is not None else "unknown",
        observed_subtask or "-",
        await _origin_label(printer_id, observed_subtask),
    )
    # Success is deliberately NOT a notification (the 2026-08-10 notification diet):
    # nothing is asked of anyone. The fleet summary names the count.
    _record(printer_id, "resumed")


async def _confirm_running(printer_id: int) -> bool:
    """Wait for RUNNING, but give up the moment the firmware says the resume FAILED.

    ``0300_400D`` ("Resume failed after power loss") is the catalog's own verdict on
    the command we just sent. Waiting out the remaining confirm budget after it appears
    would only delay the page; and a retry is exactly what the firmware just refused.
    """
    deadline = asyncio.get_running_loop().time() + _POWER_LOSS_CONFIRM_S
    while asyncio.get_running_loop().time() < deadline:
        state = printer_manager.get_status(printer_id)
        if state is not None:
            if (getattr(state, "state", None) or "").upper() == "RUNNING":
                return True
            if power_loss_resume_failed(getattr(state, "hms_errors", None) or []):
                logger.warning(
                    "[pause-recovery] printer %s reported %s — the firmware declined the resume, no retry",
                    printer_id,
                    sorted(POWER_LOSS_RESUME_FAILED_CODES),
                )
                return False
        await asyncio.sleep(_POWER_LOSS_POLL_S)
    state = printer_manager.get_status(printer_id)
    return state is not None and (getattr(state, "state", None) or "").upper() == "RUNNING"


# The two ways a resume does not take, as whole sentences. They read differently to an
# operator and neither of them is "automatic resume is off" — there is no such toggle
# (operator ruling 2026-09-04).
_HOLD_REASON_COPY: dict[str, str] = {
    "resume_refused": "The printer did not accept the resume command.",
    "resume_failed": "The printer did not restart the print after the resume was accepted.",
}


async def _hold_power_loss(printer_id: int, observed_subtask: str | None, *, reason: str) -> None:
    """The one HOLD path: a durable incident, its projection, and one page.

    Never quarantines and never retries past this point. The printer is left exactly
    where the firmware put it, with the prompt still on its screen — which is the only
    place a human can answer it now.
    """
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    state = printer_manager.get_status(printer_id)
    hms_list = getattr(state, "hms_errors", None) or []
    codes = sorted(POWER_LOSS_PROMPT_CODES)
    if power_loss_resume_failed(hms_list):
        codes += sorted(POWER_LOSS_RESUME_FAILED_CODES)

    async with async_session() as db:
        item = await _printing_farm_unit(db, printer_id, observed_subtask)
        incident = await printer_incidents.open_new(
            db,
            printer_id=printer_id,
            job_id=observed_subtask or "",
            item_id=item.id if item is not None else None,
            kind=KIND_POWER_LOSS,
            code=sorted(POWER_LOSS_PROMPT_CODES)[0],
            codes=",".join(codes),
            slot_global_tray=None,
            status=STATUS_ESCALATED,
        )
        if incident is None:
            logger.info(
                "[pause-recovery] printer %s already has an open incident — power-loss hold not opened",
                printer_id,
            )
            return
        if item is not None:
            item.waiting_reason = printer_incidents.waiting_reason_for(KIND_POWER_LOSS)
            await db.commit()

        printer = await db.get(Printer, printer_id)
        printer_name = printer.name if printer is not None else f"printer {printer_id}"
        job_name = (getattr(state, "subtask_name", None) or "").strip() or "print"
        await notification_service.on_power_loss_hold(
            printer_id=printer_id,
            printer_name=printer_name,
            job_name=job_name,
            outage_minutes=_outage_minutes(printer_id),
            reason=_HOLD_REASON_COPY[reason],
            db=db,
        )

    logger.warning(
        "[pause-recovery] printer %s HELD at the power-loss prompt (%s) — incident %s, job %s",
        printer_id,
        reason,
        incident.id,
        observed_subtask or "-",
    )
    _record(printer_id, "held")


async def _printing_farm_unit(db, printer_id: int, subtask: str | None):
    """The farm unit this hold projects onto, or None for a FOREIGN print.

    ``item_id`` NULL is the established shape for a printer held over a print the farm
    did not dispatch — the hold, the chip and the hourly reminder are printer-scoped
    and work identically without a unit to project onto.
    """
    from backend.app.services.farm_correlation import resolve_printing_item

    return await resolve_printing_item(db, printer_id, subtask)


async def _origin_label(printer_id: int, subtask: str | None) -> str:
    """ "farm" or "foreign", for the success log line only."""
    from backend.app.core.database import async_session

    async with async_session() as db:
        item = await _printing_farm_unit(db, printer_id, subtask)
    return "farm" if item is not None else "foreign"


# --- the lost-Z-reference hold (W6a's opening half) ---------------------------------


def _maybe_arm_z_reference_hold(printer_id: int, anchor: float | None) -> None:
    """Sync half of the outage arm: is this reconnect part of a FLEET outage?

    A single printer coming back is NOT this hold's trigger, deliberately. An MQTT
    session boundary is not a power cycle — the 60 s stale-reconnect, the maintenance
    toggle and the 2026-08-25 mark-plate-occupied flow all produce one — so the
    discriminator has to be a fact only an outage produces: several printers losing
    their sessions together.

    The signature is read from the FLEET's live ``PrinterState``s rather than from a
    ledger of edges this module keeps: ``disconnected_at`` is kept across the reconnect
    for exactly this purpose, so the anchors of every printer in the burst are already
    on the wire by the time the first one comes back. That is also why an arm can be
    derived at the FIRST reconnect instead of waiting for the third.
    """
    if anchor is None:
        return
    together = sum(
        1
        for st in printer_manager.get_all_statuses().values()
        if getattr(st, "disconnected_at", None) is not None
        and abs(float(st.disconnected_at) - float(anchor)) <= _OUTAGE_BURST_WINDOW_S
    )
    if together < _OUTAGE_BURST_MIN_PRINTERS:
        logger.info(
            "[pause-recovery] printer %s reconnected but only %s printer(s) lost their session together — "
            "not an outage, no lost-Z hold",
            printer_id,
            together,
        )
        return

    from backend.app.core.tasks import spawn_background_task

    spawn_background_task(
        _z_reference_arm(printer_id, together),
        name=f"z-reference-arm-p{printer_id}",
    )


async def _z_reference_arm(printer_id: int, burst_size: int) -> None:
    """Async half: hold this printer if it came back with a part on its plate.

    Runs behind the same settle as the resume driver, for the same reason — printers 1
    and 3 reconnected a SECOND time ~60 s after the first on 2026-09-04, and a state
    read on the first of those describes a printer that is about to vanish again.
    """
    try:
        await asyncio.sleep(_POWER_LOSS_SETTLE_S)
        state = printer_manager.get_status(printer_id)
        if state is None or not getattr(state, "connected", False):
            logger.info("[pause-recovery] printer %s not connected at the lost-Z check — standing down", printer_id)
            return
        live = (getattr(state, "state", None) or "").upper()
        if live in ACTIVE_PRINT_STATES:
            # A printer at the power-loss prompt is PAUSE, and the resume branch owns
            # it: a resumed print's own firmware re-homes before it moves again, so its
            # Z frame stops being fiction without anyone touching the plate.
            logger.info(
                "[pause-recovery] printer %s reconnected in %s (an active job owns it) — no lost-Z hold",
                printer_id,
                live,
            )
            return
        if not plate_occupancy.is_plate_occupied(printer_id):
            logger.info(
                "[pause-recovery] printer %s reconnected with a clear plate — no lost-Z hold (outage of %s printers)",
                printer_id,
                burst_size,
            )
            return
        await _open_z_reference_hold(
            printer_id,
            cause=f"rebooted with a part on the plate ({burst_size} printers lost their session together)",
        )
    except Exception:  # noqa: BLE001 — an assist lane must never crash its caller
        logger.exception("[pause-recovery] lost-Z arm failed for printer %s", printer_id)


async def _open_z_reference_hold(printer_id: int, *, cause: str) -> bool:
    """Open the ``z_reference_lost`` hold unless this model's Z ladder has been run.

    Fail-CLOSED on the geometry: no registry row means no witnessed Z stop for this
    machine, which is the same answer as an unvalidated row. The eject lane refuses
    while the hold stands (``eject.remote.z_reference_evidence``); ``clear_plate`` /
    ``operator_recover`` close it through :func:`on_plate_cleared`, because the human
    who takes the part off IS the resolution.
    """
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.eject.geometry import get_geometry
    from backend.app.services.notification_service import notification_service

    async with async_session() as db:
        printer = await db.get(Printer, printer_id)
        model = printer.model if printer is not None else printer_manager.get_model(printer_id)
        geometry = await get_geometry(db, model)
        if geometry is not None and geometry.z_reference_validated:
            logger.info(
                "[pause-recovery] printer %s (%s) re-references Z in its own eject block — no lost-Z hold",
                printer_id,
                geometry.model_key,
            )
            return False
        incident = await printer_incidents.open_new(
            db,
            printer_id=printer_id,
            job_id="",
            item_id=None,
            kind=KIND_Z_REFERENCE_LOST,
            code=KIND_POWER_LOSS,
            codes="",
            slot_global_tray=None,
            status=STATUS_ESCALATED,
        )
        if incident is None:
            logger.info("[pause-recovery] printer %s already has an open incident — lost-Z hold not opened", printer_id)
            return False
        printer_name = printer.name if printer is not None else f"printer {printer_id}"
        await notification_service.on_z_reference_lost(printer_id=printer_id, printer_name=printer_name, db=db)

    logger.warning(
        "[pause-recovery] printer %s HELD for a lost Z reference — %s; ejects are refused until the part "
        "is removed by hand and the plate marked cleared",
        printer_id,
        cause,
    )
    return True


# --- entry point 2: the plate-vision trip -------------------------------------------


async def on_plate_vision_trip(printer_id: int, codes: set[str]) -> bool:
    """The printer's pre-print vision check tripped: record it, then STOP the print.

    True when the print was stopped (or the stop was attempted on a printer this lane
    took ownership of); False when the lane stood aside.

    Order is the contract. The farm-abort MARK goes onto the ``printing`` row BEFORE
    the stop is sent — the same "stamp FIRST, the terminal HONOURS the mark" shape as
    ``note_eject_runtime_exceeded`` — because the terminal that the stop produces is
    the only place the unit's disposition can be decided, and an MQTT ``print.stop``
    plausibly produces the firmware's cancel echo, which would otherwise relabel the
    farm's own abort as an operator screen-stop.

    ``printer_manager.stop_print`` deliberately, NOT ``mark_printer_stopped_by_user``:
    the farm stopped this print, and claiming an operator did would defeat the mark
    this function just wrote.

    A FOREIGN print has no row to stamp; the incident IS the record (``item_id`` NULL,
    ``code`` naming the tripped check) and the terminal path reads it from the
    snapshot. Nothing here raises a plate gate or lifts the bed — both belong after the
    terminal, in ``farm_policy.on_terminal``, which is the only lane that can tell a
    first trip from a confirmed one.
    """
    try:
        from backend.app.core.database import async_session
        from backend.app.services.farm_correlation import (
            STOP_SOURCE_FARM_VISION_ABORT,
            resolve_printing_farm_item,
        )

        state = printer_manager.get_status(printer_id)
        subtask = (getattr(state, "subtask_id", None) or "").strip() or ""
        ordered = sorted(codes)

        async with async_session() as db:
            open_incident = await printer_incidents.get_open(db, printer_id)
            if open_incident is not None:
                logger.info(
                    "[pause-recovery] printer %s plate check tripped %s but an open %s incident already owns "
                    "the printer — standing aside",
                    printer_id,
                    ordered,
                    open_incident.kind,
                )
                return False

            item = await resolve_printing_farm_item(db, printer_id)
            incident = await printer_incidents.open_new(
                db,
                printer_id=printer_id,
                job_id=subtask,
                item_id=item.id if item is not None else None,
                kind=KIND_PLATE_VISION,
                code=ordered[0] if ordered else "",
                codes=",".join(ordered),
                slot_global_tray=None,
                status=STATUS_RECOVERING,
            )
            if incident is None:
                logger.info(
                    "[pause-recovery] printer %s plate check tripped %s but its incident was opened by "
                    "another actor — standing aside",
                    printer_id,
                    ordered,
                )
                return False
            if item is not None:
                item.stop_source = STOP_SOURCE_FARM_VISION_ABORT
                await db.commit()

        logger.warning(
            "[pause-recovery] printer %s plate check tripped %s (%s) — stopping the print",
            printer_id,
            ordered,
            f"unit {item.id}" if item is not None else "foreign print",
        )
        await _stop_for_vision(printer_id)
        return True
    except Exception:  # noqa: BLE001 — invariant 10: never crash the status flow
        logger.exception("[pause-recovery] plate-vision trip handling failed for printer %s", printer_id)
        return False


async def _stop_for_vision(printer_id: int) -> bool:
    """Send the stop, with ONE short retry on a send that did not go out."""
    if printer_manager.stop_print(printer_id):
        return True
    logger.info(
        "[pause-recovery] printer %s stop_print send returned False — one retry in %.0fs",
        printer_id,
        _VISION_STOP_RETRY_S,
    )
    await asyncio.sleep(_VISION_STOP_RETRY_S)
    if printer_manager.stop_print(printer_id):
        return True
    logger.warning(
        "[pause-recovery] printer %s did not accept the plate-check stop — the job stays PAUSEd and the "
        "incident holds the printer for a human",
        printer_id,
    )
    return False


# --- entry point 3: the operator's clear --------------------------------------------


async def on_plate_cleared(printer_id: int) -> bool:
    """Close a hold whose resolution IS the operator taking the part off the plate.

    Called from the clear-plate and operator-recover routes. Scoped by the incident
    model's own ``RESOLVES_ON`` table rather than by a kind list spelled here: the two
    kinds that resolve on an operator act (``plate_vision`` confirmed and
    ``z_reference_lost``) are exactly the ones whose evidence a human produces, and the
    wire-resolved kinds must NOT be closed by this — a runout hold is not answered by
    somebody clearing a plate.
    """
    try:
        from backend.app.core.database import async_session

        async with async_session() as db:
            incident = await printer_incidents.get_open(db, printer_id)
            if incident is None:
                return False
            # Read off the row BEFORE the close commits — an expiring session would
            # otherwise make the log line re-fetch a row it no longer needs.
            kind = incident.kind
            if not printer_incidents.resolves_on_operator(kind):
                logger.info(
                    "[pause-recovery] printer %s plate cleared, but its open %s incident resolves on the "
                    "wire — left standing",
                    printer_id,
                    kind,
                )
                return False
            await printer_incidents.close(db, incident.id, status=STATUS_RESOLVED, source=RESOLVE_OPERATOR)
        logger.info(
            "[pause-recovery] printer %s %s hold closed — the operator cleared the plate",
            printer_id,
            kind,
        )
        return True
    except Exception:  # noqa: BLE001 — an operator verb must never fail on its hold cleanup
        logger.exception("[pause-recovery] plate-cleared hold close failed for printer %s", printer_id)
        return False


# --- the fleet summary --------------------------------------------------------------


def _outage_minutes(printer_id: int) -> int | None:
    """Whole minutes this printer was off the wire, or None when we never saw it go.

    None is deliberate and is rendered as "no duration sentence" rather than as zero:
    a restart mid-outage erases the measurement, and a plausible wrong number in a
    notification is worse than a missing one.
    """
    sample = _seen.get(printer_id)
    if sample is None or sample.outage_s is None:
        return None
    return int(sample.outage_s // 60)


def _record(printer_id: int, bucket: str) -> None:
    """Add one printer to the open outage window, opening the window if needed."""
    global _summary, _summary_task

    if _summary is None:
        _summary = _OutageSummary(opened_at=time.time())
        from backend.app.core.tasks import spawn_background_task

        _summary_task = spawn_background_task(_close_summary_window(), name="power-loss-summary")
    getattr(_summary, bucket).add(printer_id)
    outage_s = _seen.get(printer_id).outage_s if _seen.get(printer_id) is not None else None
    if outage_s is not None and (_summary.outage_s is None or outage_s > _summary.outage_s):
        # The LONGEST outage in the window is the one that describes it: printers come
        # back at their own pace, and the fleet was down for however long the last one
        # took.
        _summary.outage_s = outage_s


async def _close_summary_window() -> None:
    """Emit ONE summary per outage, and ALWAYS log the close.

    The log line is not decoration: a page suppressed by a disabled provider and a
    lane that never ran are indistinguishable on notification history alone, and this
    lane's whole failure mode is silence (F9).
    """
    global _summary, _summary_task
    try:
        await asyncio.sleep(_SUMMARY_WINDOW_S)
        summary, _summary = _summary, None
        if summary is None:
            return
        minutes = int(summary.outage_s // 60) if summary.outage_s is not None else None
        logger.info(
            "[pause-recovery] outage window closed — resumed=%s held=%s stopped_ejects=%s held_by_fault=%s outage=%s",
            sorted(summary.resumed),
            sorted(summary.held),
            sorted(summary.stopped_ejects),
            sorted(summary.held_by_fault),
            f"~{minutes} min" if minutes is not None else "unknown",
        )
        from backend.app.core.database import async_session
        from backend.app.services.notification_service import notification_service

        async with async_session() as db:
            await notification_service.on_power_loss_recovery_summary(
                resumed=len(summary.resumed),
                held=len(summary.held),
                stopped_ejects=len(summary.stopped_ejects),
                held_by_fault=len(summary.held_by_fault),
                outage_minutes=minutes,
                db=db,
            )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a summary failure must not take the lane down
        logger.exception("[pause-recovery] outage summary failed")
    finally:
        _summary_task = None
