"""The session start — THE one re-evaluation of a printer when it begins a new MQTT session.

A restart, a reconnect, a power cycle: the farm stopped seeing a printer and now sees it again.
What happened meanwhile is answered by the printer's FIRST FRESH REPORT of the new session
(``printer_events.FirstFreshReport``, stamped once per session by the transport), and by nothing
earlier. Until 2026-10-11 six triggers answered it at different moments: ``main.lifespan`` ran the
incident sweep, the plate-check deadline re-arm and the pending-eject reconcile BEFORE
``init_printer_connections`` — so every printer read "unreported" (the 001-H2S hold stood all night
over a toolhead the wire reported fed) — while ``main.on_printer_status_change`` spawned the
downtime reconcile and the eject reconcile behind an epoch bookmark of its own, and
``pause_recovery`` armed the lost-Z hold on a reconnect's FIRST push, which re-broadcasts the
previous session's cached state.

Now ONE subscriber, :meth:`SessionStart.on_first_fresh_report`, runs every lane's session decision
for that printer, in this order, on every session (a restart's first included):

1. the downtime reconcile (``main.reconcile_stale_active_prints``, INJECTED — a service never
   imports ``main``): the live archive judged against what the printer runs now. First, so a
   terminal it synthesises is classified against the holds that stood when the job ended, and its
   ``JobTerminal`` answers the record's stop or start of that job before step 2 can void it;
2. the outstanding actions (``printer_action.reread_session``): every command the record still holds
   unanswered re-reads the printer — answered by what the printer reports now, or VOID if the
   session that carried it ended unacknowledged and the report answers nothing. Before the holds,
   so they read the record's answers (the incident projection's pending command among them);
   …then the dispatch claims (``dispatch_start.reevaluate_session``): a start the record answered
   before this process could apply it is applied, and a claim with no start on the record is judged
   against this report;
   …and the reads the farm commanded before the gap (``ams_presence.note_session_start``): answered
   by this report — a read still running shows as identifying on it;
3. the open holds (``spool_recovery.reevaluate_session``): each row's ``startup`` cell, the resume
   owed, and the re-entry of a ``recovering`` row whose driver a restart or a crash killed;
4. the plate-check decision deadline (``pause_recovery.rearm_decision_deadlines``);
5. a pending eject nobody owns (``eject.monitor.reconcile_pending_eject``);
6. the lost-Z arm (``pause_recovery.note_session_start``).

Each step is its lane's own decision, re-reading the live state and idempotent; this module owns
only WHEN they run and their ORDER. One task per printer session; a newer session of the same
printer waits for the older one's steps to finish, so two sessions never decide one printer at
once. Every step is guarded: one lane's failure never costs the others their turn.

Process memory: the running task per printer (:attr:`SessionStart._sessions`). A restart kills it,
which is the truth, and the next first fresh report runs every step again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from backend.app.core.tasks import spawn_background_task
from backend.app.services import ams_presence, dispatch_start, pause_recovery, printer_action, spool_recovery
from backend.app.services.eject import monitor as eject_monitor
from backend.app.services.printer_events import FirstFreshReport

logger = logging.getLogger(__name__)


class SessionStart:
    """The ``FirstFreshReport`` subscriber (wired by ``printer_event_wiring``). ``downtime_reconcile``
    is the downtime reconcile's body, injected by the composition root (``main`` owns the full
    terminal it runs)."""

    def __init__(self, *, downtime_reconcile: Callable[[int], Awaitable[object]]) -> None:
        self._downtime_reconcile = downtime_reconcile
        self._sessions: dict[int, asyncio.Task[None]] = {}

    def on_first_fresh_report(self, event: FirstFreshReport) -> None:
        """Start ``event``'s session re-evaluation — behind the printer's previous one, if it still runs."""
        previous = self._sessions.get(event.printer_id)
        self._sessions[event.printer_id] = spawn_background_task(
            self._run(event, previous), name=f"session-start-p{event.printer_id}"
        )

    async def _run(self, event: FirstFreshReport, previous: asyncio.Task[None] | None) -> None:
        printer_id = event.printer_id
        try:
            if previous is not None and not previous.done():
                await asyncio.wait({previous})
            logger.info(
                "session_start: printer %s session %s — first fresh report, re-evaluating",
                printer_id,
                event.connection_epoch,
            )
            await self._step(printer_id, "downtime reconcile", self._downtime_reconcile(printer_id))
            await self._step(
                printer_id, "outstanding actions", printer_action.reread_session(printer_id, event.connection_epoch)
            )
            await self._step(printer_id, "dispatch claims", dispatch_start.reevaluate_session(printer_id))
            try:
                ams_presence.note_session_start(printer_id)
            except Exception:  # noqa: BLE001 — one lane must never cost the others their session start
                logger.exception("session_start: printer %s pending reads failed", printer_id)
            await self._step(printer_id, "open holds", spool_recovery.reevaluate_session(printer_id))
            await self._step(printer_id, "decision deadline", pause_recovery.rearm_decision_deadlines(printer_id))
            await self._step(printer_id, "pending eject", eject_monitor.reconcile_pending_eject(printer_id))
            pause_recovery.note_session_start(printer_id, event.connection_epoch)
        finally:
            if self._sessions.get(printer_id) is asyncio.current_task():
                del self._sessions[printer_id]

    @staticmethod
    async def _step(printer_id: int, label: str, step: Awaitable[object]) -> None:
        """Await one lane's session decision; its failure is logged, and the next lane still runs."""
        try:
            await step
        except Exception:  # noqa: BLE001 — one lane must never cost the others their session start
            logger.exception("session_start: printer %s %s failed", printer_id, label)
