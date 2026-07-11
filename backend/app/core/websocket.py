import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections and broadcasts."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        """Accept a new WebSocket connection."""
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)

    async def disconnect(self, websocket: WebSocket):
        """Remove a WebSocket connection."""
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)

    async def broadcast(self, message: dict[str, Any]):
        """Broadcast a message to all connected clients."""
        if not self.active_connections:
            return

        data = json.dumps(message)
        async with self._lock:
            disconnected = []
            for connection in self.active_connections:
                try:
                    await connection.send_text(data)
                except Exception:
                    disconnected.append(connection)

            # Clean up disconnected clients
            for conn in disconnected:
                if conn in self.active_connections:
                    self.active_connections.remove(conn)

    async def send_printer_status(self, printer_id: int, status: dict):
        """Send printer status update to all clients."""
        await self.broadcast(
            {
                "type": "printer_status",
                "printer_id": printer_id,
                "data": status,
            }
        )

    async def send_print_start(self, printer_id: int, data: dict):
        """Notify clients that a print has started."""
        await self.broadcast(
            {
                "type": "print_start",
                "printer_id": printer_id,
                "data": data,
            }
        )

    async def send_print_complete(self, printer_id: int, data: dict):
        """Notify clients that a print has completed."""
        await self.broadcast(
            {
                "type": "print_complete",
                "printer_id": printer_id,
                "data": data,
            }
        )

    async def send_archive_created(self, archive: dict):
        """Notify clients that a new archive was created."""
        await self.broadcast(
            {
                "type": "archive_created",
                "data": archive,
            }
        )

    async def send_archive_updated(self, archive: dict):
        """Notify clients that an archive was updated."""
        await self.broadcast(
            {
                "type": "archive_updated",
                "data": archive,
            }
        )

    async def send_missing_spool_assignment(
        self,
        printer_id: int,
        printer_name: str,
        missing_slots: list[dict[str, str]],
    ):
        """Notify clients that a print started with missing spool assignments."""
        await self.broadcast(
            {
                "type": "missing_spool_assignment",
                "printer_id": printer_id,
                "printer_name": printer_name,
                "missing_slots": missing_slots,
            }
        )


# Global connection manager
ws_manager = ConnectionManager()


def broadcast_production_run_changed(run_id: int) -> None:
    """Fire-and-forget farm event: a production run's derived state changed (Phase 4).

    ONE event type covers every run mutation (pause/resume/abort, operator stop,
    quarantine auto-pause, first-article transitions, completion, top-up, stall
    flag changes) — the frontend reacts with a single debounced
    ``['production-runs']`` invalidation, mirroring how the printer flags reuse
    ``printer_status``. Module-level (not a ConnectionManager method) so farm
    services can import ONE name and tests can spy on it per call site.

    Guarded twice: no running event loop (sync unit tests, CLI) → silent no-op
    without ever creating the coroutine (avoids "never awaited" warnings), and
    any scheduling error is logged, never raised — a WS hiccup must not abort a
    committed run transition.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop → nothing to notify (sync test / script context)
    try:
        from backend.app.core.tasks import spawn_background_task

        spawn_background_task(
            ws_manager.broadcast({"type": "production_run_changed", "run_id": run_id}),
            name=f"ws-production-run-changed-{run_id}",
        )
    except Exception as e:  # noqa: BLE001 — a broadcast failure must never break the caller
        logger.warning("Failed to broadcast production_run_changed for run %d: %s", run_id, e)
