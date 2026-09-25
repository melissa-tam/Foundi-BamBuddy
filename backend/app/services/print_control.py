"""The operator's STOP, as ONE verb.

``stop_as_operator`` is the operator's stop: record the stop REQUEST on the unit the
printer is running **and** send ``print.stop`` over MQTT, in that order. The request is
what makes the resulting terminal an operator CANCEL rather than a failure —
``main.on_print_complete`` reads it back off the row (``farm_correlation
.operator_stop_requested`` → ``classify_stop``'s ``operator_ui`` verdict) and records the
firmware's ``failed``/``aborted`` as ``cancelled``, which is what keeps a stop out of the
retry/quarantine accounting.

**Durable since 2026-09-25.** The request used to be a process-memory mark
(``main._user_stopped_printers``). A restart — a deploy — emptied it, and the terminal of
a job an operator had stopped then read as no stop at all: the farm's own job resolved
FOREIGN (a foreign page, a human-clear gate, no operator-stop hold, a run one plate
short). It now lives on the unit row (``print_queue.operator_stop_requested_at``, written
by ``queue_transitions.stamp_operator_stop``), where a restart cannot reach it.

**The order is stamp, COMMIT, publish.** A terminal can only follow the publish, so a
request committed before the publish is read by every terminal the stop produces, however
soon it lands. The old verb was sync and published first because the mark had to be set
before the caller's next ``await`` yielded to the terminal callback; a committed row closes
that window by ordering instead, so the verb is a coroutine now and every caller awaits it.

**Which unit.** The one the queue page names (``unit_id`` — it knows exactly which row its
Stop is for) or else the printer's in-flight unit by ``farm_correlation
.resolve_printing_item``, the ONE in-flight attribution (the printer's live job id first,
the sole printing unit otherwise). A FOREIGN print has no unit and so no request: its
terminal is classified from the printer's own cancel echo, exactly as a touchscreen stop
is. The request stands whether or not the publish was delivered — an operator asked the
print to stop, and an undelivered stop still reports itself (the printer-card route's
502) — until the unit ends or is re-armed (``queue_transitions.release_unstarted_claim``).

**The caller set is CLOSED, and every member is an operator pressing Stop:** the
printer-card route and the printer's own HMS dialog "Stop printing"
(``api/routes/printers.py``), the queue-page route (``api/routes/print_queue.py``) and
the API clients' ``/stop`` and ``/cancel`` (``api/routes/webhook.py``). The service-hold
quiesce was deleted on 2026-09-19 with the ruling that **no mode verb ends a print**:
entering maintenance mode, or deactivating a printer, stands the FARM down, and a running
print is the operator's, not the farm's. ``test_code_quality.TestOperatorStopOwnership``
pins the callers by AST, so a future mode verb that reaches for this pair fails CI instead
of silently cancelling somebody's plate — and a sibling pin allows a RAW ``stop_print`` only
in this module and the eject lane.

What it deliberately does NOT own: the queue row's END (status, ``stop_source``,
``error_message``), the smart-plug auto-off and the HTTP shapes. Those differ per caller and
stay with the caller — this module is the stop REQUEST plus the WIRE act, nothing else.

**Never raises**, and returns whether the stop was DELIVERED. A caller that needs to report
undelivered (the printer-card route's 502) reads the bool. ``printer_manager.stop_print``
answers False for a printer with no live client, so "no session" and "publish refused" are
one answer here — they are one answer to the operator too. A request that could not be
recorded is logged and the stop is still sent: losing the request mislabels a cancel as a
failure, which is worse than a log line but not a reason to leave the print running.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from backend.app.core.database import run_with_retry
from backend.app.services.farm_correlation import resolve_printing_item
from backend.app.services.printer_manager import printer_manager
from backend.app.services.queue_transitions import stamp_operator_stop

logger = logging.getLogger(__name__)


async def _record_stop_request(printer_id: int, unit_id: int | None) -> int | None:
    """Stamp the stop request on the unit this stop is for, and COMMIT it. The unit's id, or None.

    None when there is no unit to stamp (a foreign print, an idle printer, a pathological
    several-printing state with no live-id match) or the write failed — the stop is sent anyway.
    """
    requested_at = datetime.now(timezone.utc)

    async def _write(db) -> int | None:
        target = unit_id
        if target is None:
            live = printer_manager.get_status(printer_id)
            item = await resolve_printing_item(db, printer_id, getattr(live, "subtask_id", None))
            target = item.id if item is not None else None
        if target is None or not await stamp_operator_stop(db, target, requested_at=requested_at):
            return None
        await db.commit()
        return target

    try:
        return await run_with_retry(_write, label="operator stop request")
    except Exception:  # noqa: BLE001 — the request is best-effort; the stop below still goes out
        logger.exception("print_control: could not record the stop request for printer %s", printer_id)
        return None


async def stop_as_operator(printer_id: int, *, unit_id: int | None = None) -> bool:
    """Stop the current print AS THE OPERATOR. True iff the stop was delivered.

    ``unit_id`` — the unit the caller's Stop is for, when the caller knows it (the queue
    page); otherwise the printer's in-flight unit. Its request is committed BEFORE the
    publish, so the terminal the stop produces always finds it.
    """
    stamped = await _record_stop_request(printer_id, unit_id)
    if stamped is not None:
        logger.info("print_control: printer %s — operator stop requested for unit %s", printer_id, stamped)

    delivered = False
    try:
        delivered = printer_manager.stop_print(printer_id)
    except Exception:  # noqa: BLE001 — a stop is best-effort; the caller reports it undelivered
        logger.exception("print_control: stop_print raised for printer %s", printer_id)
    if not delivered:
        logger.warning(
            "print_control: printer %s stop NOT delivered (no live MQTT session or publish refused)", printer_id
        )
    return delivered
