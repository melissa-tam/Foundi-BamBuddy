"""Thin websocket progress emitters for dispatch + eject (latency Phase C3/C4).

The UI otherwise looks dead during the slow FTPS upload + start handshake. These
emitters broadcast two fine-grained event types over the existing
``ws_manager.broadcast`` so the frontend can show "Uploading 62% / Sent /
Preparing…" instead of a frozen card.

The envelope shapes are a CONTRACT with the frontend — do NOT rename fields::

    {"type":"queue_item_status","item_id":123,"batch_id":45,"printer_id":7,
     "status":"pending|printing|...","phase":"assigned|uploading|sent|preparing|printing|failed",
     "progress_pct":62.0,"detail":null,"ts":"<iso-utc>"}

    {"type":"eject_progress","printer_id":7,"queue_item_id":123,
     "phase":"building|uploading|sent|sweeping|failed","progress_pct":null,"detail":null,"ts":"<iso-utc>"}

``uploading`` events are per-key (item or printer) throttled to ~1 Hz so a fast
upload doesn't flood the socket; every terminal / phase-change event always passes.
Emit is fire-and-forget and guarded: no running loop (sync tests) → silent no-op;
a broadcast hiccup is logged, never raised — telemetry must never break a dispatch.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from backend.app.core.websocket import ws_manager

logger = logging.getLogger(__name__)

_UPLOAD_MIN_INTERVAL_S = 1.0

# Last ``uploading`` emit time per throttle key (``("item", id)`` / ``("eject", printer)``).
_last_upload_emit: dict[tuple[str, int], float] = {}


def _throttle_ok(key: tuple[str, int], phase: str) -> bool:
    """True if this event may emit. ``uploading`` is throttled to ~1 Hz per key; any
    other (phase-change / terminal) phase always passes AND resets the key's window so
    the next upload burst starts fresh."""
    if phase != "uploading":
        _last_upload_emit.pop(key, None)
        return True
    now = time.monotonic()
    last = _last_upload_emit.get(key)
    if last is not None and (now - last) < _UPLOAD_MIN_INTERVAL_S:
        return False
    _last_upload_emit[key] = now
    return True


def phase_for_observed_state(state: str | None) -> str | None:
    """Map a printer gcode_state to a dispatch progress phase (used by the start
    watchdog): PREPARE/SLICING → ``preparing``, RUNNING → ``printing``, else None."""
    if state in ("PREPARE", "SLICING"):
        return "preparing"
    if state == "RUNNING":
        return "printing"
    return None


def _broadcast(message: dict) -> None:
    """Fire-and-forget broadcast, timestamped, guarded twice (no loop → no-op; any
    scheduling error logged, never raised). Mirrors ``broadcast_production_run_changed``."""
    message["ts"] = datetime.now(timezone.utc).isoformat()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop (sync test / script) → nothing to notify
    try:
        from backend.app.core.tasks import spawn_background_task

        spawn_background_task(ws_manager.broadcast(message), name=f"ws-{message['type']}")
    except Exception as exc:  # noqa: BLE001 — a broadcast failure must never break the caller
        logger.warning("progress: failed to broadcast %s: %s", message.get("type"), exc)


def emit_queue_item_status(
    *,
    item_id: int,
    batch_id: int | None,
    printer_id: int | None,
    status: str,
    phase: str,
    progress_pct: float | None = None,
    detail: str | None = None,
) -> None:
    """Broadcast a ``queue_item_status`` dispatch-progress event (throttled for
    ``uploading``)."""
    if not _throttle_ok(("item", item_id), phase):
        return
    _broadcast(
        {
            "type": "queue_item_status",
            "item_id": item_id,
            "batch_id": batch_id,
            "printer_id": printer_id,
            "status": status,
            "phase": phase,
            "progress_pct": progress_pct,
            "detail": detail,
        }
    )


def emit_eject_progress(
    *,
    printer_id: int,
    queue_item_id: int | None,
    phase: str,
    progress_pct: float | None = None,
    detail: str | None = None,
) -> None:
    """Broadcast an ``eject_progress`` event (throttled for ``uploading``)."""
    if not _throttle_ok(("eject", printer_id), phase):
        return
    _broadcast(
        {
            "type": "eject_progress",
            "printer_id": printer_id,
            "queue_item_id": queue_item_id,
            "phase": phase,
            "progress_pct": progress_pct,
            "detail": detail,
        }
    )
