"""Printer events — the ONE dispatcher of what a printer told the farm (GoF Observer), a LEAF.

Every fact below already had exactly ONE producer; what it did not have was one way to reach the
farm lanes that act on it. ``main.on_printer_status_change`` hand-called seven samplers in a fixed
order, the startup sweep decided holds before any printer had reported (the 001-H2S stranding,
2026-10-11: ``rearm_incidents_on_startup`` ran at ``main.lifespan`` BEFORE
``init_printer_connections``, so every printer read "unreported" and nothing was re-evaluated once
it did report), and a waiter that wanted a command's ACK polled a deque once a second. This hub
RE-PUBLISHES those producers' facts — it never re-derives one:

* :class:`CommandAcked` — the firmware's ACK, from ``BambuMQTTClient._record_command_ack`` (the
  one writer of ``PrinterState.command_acks``, invariant 14);
* :class:`StatusReported` — one status push, from ``main.on_printer_status_change`` (the receiver
  of the client's status callback), once per push;
* :class:`HmsEdgesAppeared` — the HMS codes that APPEARED on that push, from main's ONE
  ``hms_edges.note_push`` call;
* :class:`FeedEventReported` — a toolhead feed-state event (roll drained, load landed,
  auto-switched, air began), from ``BambuMQTTClient._step_feed`` (the one stepper of
  ``feed_state.FeedTracker``, invariant 16);
* :class:`JobTerminal` — a job's terminal, from the ONE terminal entry ``main.on_print_complete``,
  so a terminal the downtime reconcile SYNTHESISES publishes exactly like a live one;
* :class:`FirstFreshReport` — the printer's first fresh report of an MQTT session, from the ONE
  ``PrinterState.report_epoch`` stamp in ``BambuMQTTClient._update_state``;
* :class:`SessionLost` — the printer's MQTT session ended, from the ONE writer of the outage anchor,
  ``BambuMQTTClient.note_disconnected`` (on its connected → disconnected edge). The record's online
  clock stops counting there; the next session's first fresh report restarts it;
* :class:`ActionAnswered` — the record answered one of its actions (``printer_action._answer``, the
  one writer of an answer): the printer's answer to a command, read through the outcome tables
  from the events above — the farm fact a lane acts on (the AMS ran the farm's queued command);
* :class:`SelfHealDue` — an outstanding action's self-heal bound passed in ONLINE time with no event
  answering it (``printer_action._pass_bound``, the one writer of ``bound_passed_at``): the lane that
  owns the action takes its fallback. The action stays outstanding, so a late answer still lands as
  :class:`ActionAnswered`.

A client-side fact reaches the hub through the registry (``printer_manager`` builds the event in
the closure that knows the printer id and hops onto the event loop), exactly as the client's other
callbacks do; ``main`` publishes the two facts it computes. Each event class is constructed at ONE
site (``test_printer_events``), and only ``printer_event_wiring`` — the composition root — calls
:func:`subscribe`, so the ORDER every handler runs in is written in one place.

**Dispatch.** :func:`publish` runs on the event loop and calls every handler of the event's kind in
subscription order: a sync handler is called, an async handler is AWAITED before the next one runs.
That is the order the hand-written fan-out had (it awaited the runout-guidance refresh inline), and
it keeps "the samplers ran on this push" true when the push's own handler returns. A handler that
must not hold up the others spawns its work through ``core.tasks.spawn_background_task`` itself,
never a bare ``asyncio.create_task``. A handler that raises is logged and the others still run:
nothing farm-side may break the status chain (invariant 10). A producer on the MQTT network thread
never calls :func:`publish` directly — the registry schedules it onto the loop.

Stdlib only at runtime (``test_import_graph``): the event fields name the producers' own types under
``TYPE_CHECKING``, so importing the hub loads nothing farm-side and closes no cycle.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from backend.app.services.bambu_mqtt import CommandAck, PrinterState
    from backend.app.services.feed_state import AirBegan, AutoSwitched, FeedReading, LoadLanded, RollDrained
    from backend.app.services.hms_edges import HmsEdgeReport

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrinterEvent:
    """What every event carries: the printer it is about (the DB id — the client is keyed by
    serial, so the registry that knows both builds the client's events)."""

    printer_id: int


@dataclass(frozen=True)
class CommandAcked(PrinterEvent):
    """The firmware answered a command: the :class:`~backend.app.services.bambu_mqtt.CommandAck`
    just appended to ``PrinterState.command_acks`` — its ``sequence_id`` names the send it
    answers."""

    ack: CommandAck


@dataclass(frozen=True)
class StatusReported(PrinterEvent):
    """One status push, as the status callback received it. ``state`` is the client's LIVE
    ``PrinterState`` — read it while handling, never keep it (the next push mutates it)."""

    state: PrinterState


@dataclass(frozen=True)
class HmsEdgesAppeared(PrinterEvent):
    """HMS codes APPEARED on this push (``hms_edges.note_push``'s report — published only when it
    reports an edge). The edge is the state decision's trigger; re-notify is ``notify_dedup``'s."""

    edges: HmsEdgeReport
    state: PrinterState


@dataclass(frozen=True)
class FeedEventReported(PrinterEvent):
    """The toolhead feed state produced an event on a push (invariant 16): ``event`` is the
    tracker's own object, ``reading`` the snapshot that step produced."""

    event: RollDrained | LoadLanded | AutoSwitched | AirBegan
    reading: FeedReading


@dataclass(frozen=True)
class JobTerminal(PrinterEvent):
    """A job ended, live or synthesised by the downtime reconcile (``reconciled``).

    ``status`` is the printer's own word, ``recorded_status`` the terminal outcome's
    (``terminal_outcome.build_terminal_outcome``); ``job_id`` the echoed ``subtask_id``
    (compare it through ``job_identity``, never by hand); ``is_eject`` main's eject detection;
    ``archive_id`` the record the reconcile bound it to (None for a live terminal); ``payload``
    the terminal payload as it arrived."""

    status: str
    recorded_status: str
    job_id: str | None
    is_eject: bool
    reconciled: bool
    archive_id: int | None
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class ActionAnswered(PrinterEvent):
    """The record (``services/printer_action``) answered action ``action_id`` FINALLY: ``answer`` is
    ``command_outcome.Answer``'s token (reached / refused / void / taken_over), ``command`` the record's
    command name, ``actor`` who sent it, ``incident_id`` the hold it acted for (``None`` for none),
    ``reason`` the record's reason (a refusal's printer words; ``None`` for none)."""

    action_id: int
    command: str
    actor: str
    incident_id: int | None
    answer: str
    reason: str | None = None


@dataclass(frozen=True)
class FirstFreshReport(PrinterEvent):
    """The printer's first fresh report of the MQTT session ``connection_epoch`` has been applied
    (``PrinterState.report_epoch == connection_epoch``): from here the state describes the printer
    NOW, not the previous session's cache the reconnect re-broadcast. Once per session."""

    connection_epoch: int


@dataclass(frozen=True)
class SessionLost(PrinterEvent):
    """The printer's MQTT session ended — a disconnect, a forced reconnect, a teardown, a power cut
    (``BambuMQTTClient.note_disconnected``, on the edge only). Offline is THIS fact, never a quiet
    report: a printer that reports nothing for a while is still online."""


@dataclass(frozen=True)
class SelfHealDue(PrinterEvent):
    """Action ``action_id`` (record command ``command``) is still unanswered past its self-heal bound
    ``bound_name``, counted in the printer's ONLINE seconds (``printer_action``). Published once per
    action, when the bound first passes; ``incident_id`` / ``queue_item_id`` name the hold or the unit
    it acted for (``None`` for none). The action is NOT answered by it."""

    action_id: int
    command: str
    bound_name: str
    incident_id: int | None
    queue_item_id: int | None


E = TypeVar("E", bound=PrinterEvent)

Handler = Callable[[Any], Awaitable[None] | None]

_subscribers: dict[type[PrinterEvent], list[Handler]] = {}


def subscribe(kind: type[E], handler: Callable[[E], Awaitable[None] | None]) -> Callable[[], None]:
    """Run ``handler`` for every published event of exactly ``kind``, after the handlers already
    subscribed to it. Subscribing the same handler twice is a no-op. Returns the unsubscribe."""
    handlers = _subscribers.setdefault(kind, [])
    if handler not in handlers:
        handlers.append(handler)

    def unsubscribe() -> None:
        current = _subscribers.get(kind)
        if current is not None and handler in current:
            current.remove(handler)

    return unsubscribe


def subscribers(kind: type[PrinterEvent]) -> tuple[Handler, ...]:
    """The handlers ``kind`` dispatches to, in order (what :func:`publish` will run)."""
    return tuple(_subscribers.get(kind, ()))


async def publish(event: PrinterEvent) -> None:
    """Dispatch ``event`` to its kind's handlers, in subscription order (module docstring). Never
    raises: a handler's exception is logged and the next handler runs."""
    for handler in subscribers(type(event)):
        try:
            result = handler(event)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 — invariant 10: one handler must never break the chain
            logger.exception(
                "printer_events: %s handler %s failed for printer %s",
                type(event).__name__,
                getattr(handler, "__qualname__", handler),
                event.printer_id,
            )
