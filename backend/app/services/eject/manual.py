"""Manual "Eject now" for a farm-known finished unit (W2).

An operator affordance to trigger the part-present eject sweep by hand — either to
skip the cooldown wait (with an explicit hot-bed confirm) or to clear a plate the
automatic watch could not. Deliberately SCOPED to farm-known parts only: the target
unit is resolved from the armed cooldown watch's identity or, failing that, from the
last completed eject-profiled unit whose dispatch subtask raised the current plate
gate. Foreign / blind sweeps are never allowed, and an unapproved first article must
still go through the approval flow.

The service composes the existing eject primitives — it NEVER hand-rolls a dispatch.
When a cooldown watch is armed it merely signals that watch's single ``_do_release``
path (``request_release_now``) so there is no parallel dispatch race; otherwise it
calls the shared ``eject_remote.dispatch_part_present_eject`` directly.

Ordered precondition checks each raise :class:`ManualEjectError` (a plain domain
error carrying a stable machine code + HTTP status hint); the route translates them
to ``HTTPException``. :class:`BedTooHot` fires only with a REAL live bed reading
above the threshold and carries bed + threshold so the UI can show the confirm
dialog with true numbers; an unreadable bed is its own ``bed_unreadable`` 409 (a
retry-in-a-moment condition, never a confirm prompt built on a missing reading).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.eject import remote as eject_remote
from backend.app.services.eject.monitor import _latest_started_item, _resolve_eject_threshold, eject_cooldown_monitor
from backend.app.services.printer_manager import printer_manager

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class ManualEjectError(RuntimeError):
    """A manual eject could not be started.

    ``code`` is a stable machine-readable reason (``not_found`` / ``not_connected``
    / ``printer_busy`` / ``no_plate_gate`` / ``eject_in_flight`` / ``no_eligible_unit``
    / ``bed_unreadable``) and ``status_code`` the HTTP hint the route applies without
    this module importing FastAPI.
    """

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class BedTooHot(ManualEjectError):
    """A REAL live bed reading is above the release threshold and the caller did not
    pass ``allow_hot`` — carries the live bed + threshold (both always finite floats)
    so the UI can render the explicit hot-bed confirm dialog with true numbers. An
    unreadable bed never raises this (it raises ``bed_unreadable`` instead)."""

    def __init__(self, bed_c: float, threshold_c: float) -> None:
        super().__init__(
            "bed_hot",
            f"Bed is {bed_c:.1f}°C, above the {threshold_c:.1f}°C eject threshold — confirm to eject hot",
            status_code=409,
        )
        self.bed_c = bed_c
        self.threshold_c = threshold_c


async def _resolve_manual_eject_item(db: AsyncSession, printer_id: int) -> int | None:
    """Resolve the farm-known unit to eject on ``printer_id``, or None if none eligible.

    Prefers the armed PRODUCTION cooldown watch's ``queue_item_id`` (the unit the
    watch is already cooling for). Falls back to the ``should_rearm``-style DB lookup:
    the most-recently started unit, which must be a COMPLETED, eject-profiled,
    NON-first-article unit whose ``dispatch_subtask_id`` matches the printer's
    ``plate_gate_subtask_id`` (the gate this eject would clear). An unapproved first
    article is deliberately excluded — it must use the approval flow."""
    identity = eject_cooldown_monitor.active_watch_identity(printer_id)
    if identity is not None and identity.purpose == "production" and identity.queue_item_id is not None:
        return identity.queue_item_id

    printer = await db.get(Printer, printer_id)
    item = await _latest_started_item(db, printer_id)
    if printer is None or item is None:
        return None
    if item.status != "completed" or item.eject_profile_id is None or item.first_article:
        return None
    if not printer.plate_gate_subtask_id or item.dispatch_subtask_id != printer.plate_gate_subtask_id:
        return None
    return item.id


async def manual_eject(db: AsyncSession, printer_id: int, *, allow_hot: bool = False) -> dict:
    """Trigger a part-present eject for a farm-known finished unit on ``printer_id``.

    Ordered 409 preconditions: printer known → connected → not RUNNING/PAUSE → plate
    gate raised → no eject already in flight → a farm-known eligible unit resolves →
    thermal check (skipped entirely by ``allow_hot``): an unreadable live bed is a
    retryable ``bed_unreadable`` 409; a real reading above the release threshold is
    ``BedTooHot``. Then:

    * an armed cooldown watch → signal its single release path (no parallel dispatch);
    * otherwise → dispatch the eject directly.

    Returns ``{"mode": "released_watch"|"dispatched", "queue_item_id": int}``.
    """
    printer = await db.get(Printer, printer_id)
    if printer is None:
        raise ManualEjectError("not_found", "Printer not found", status_code=404)
    if not printer_manager.is_connected(printer_id):
        raise ManualEjectError("not_connected", "Printer is not connected; cannot eject", status_code=409)

    state = printer_manager.get_status(printer_id)
    if state is not None and getattr(state, "state", None) in ("RUNNING", "PAUSE"):
        raise ManualEjectError("printer_busy", "Printer is printing or paused; cannot eject now", status_code=409)

    if not printer_manager.is_awaiting_plate_clear(printer_id):
        raise ManualEjectError(
            "no_plate_gate", "Printer is not awaiting plate clear; nothing to eject", status_code=409
        )

    if eject_remote.peek_pending_eject(printer_id) is not None:
        raise ManualEjectError("eject_in_flight", "An eject is already in flight on this printer", status_code=409)

    queue_item_id = await _resolve_manual_eject_item(db, printer_id)
    if queue_item_id is None:
        raise ManualEjectError(
            "no_eligible_unit",
            "No farm-known finished unit to eject on this printer (first articles use the approval flow)",
            status_code=409,
        )

    threshold = await _resolve_eject_threshold(queue_item_id)
    if threshold is None:
        raise ManualEjectError("no_eligible_unit", "Unit has no eject profile; cannot eject", status_code=409)

    bed = state.temperatures.get("bed") if state is not None and getattr(state, "connected", False) else None
    if not allow_hot:
        if bed is None:
            # No live reading (e.g. the brief post-reconnect telemetry window): a
            # retryable condition, NOT a hot-bed confirm — a confirm dialog built on
            # a missing reading would show fabricated temperatures to the operator.
            raise ManualEjectError(
                "bed_unreadable",
                "Live bed temperature is unavailable; wait a few seconds for printer telemetry and retry",
                status_code=409,
            )
        if bed > threshold:
            raise BedTooHot(bed, threshold)

    # Armed PRODUCTION watch → drive its single _do_release path (no parallel race).
    identity = eject_cooldown_monitor.active_watch_identity(printer_id)
    if identity is not None and identity.purpose == "production" and identity.queue_item_id == queue_item_id:
        if eject_cooldown_monitor.request_release_now(printer_id):
            logger.info(
                "manual_eject: signalled immediate release on printer %s (watch armed, item %s)",
                printer_id,
                queue_item_id,
            )
            return {"mode": "released_watch", "queue_item_id": queue_item_id}

    # No armed watch (the DB-fallback path) → dispatch directly. EjectDispatchError
    # propagates for the route to translate to its status hint.
    item = await db.get(PrintQueueItem, queue_item_id)
    run_id = item.batch_id if item is not None else None
    await eject_remote.dispatch_part_present_eject(
        db, printer_id=printer_id, queue_item_id=queue_item_id, purpose="production", run_id=run_id
    )
    logger.info("manual_eject: dispatched part-present eject on printer %s for item %s", printer_id, queue_item_id)
    return {"mode": "dispatched", "queue_item_id": queue_item_id}
