"""The printer-event hub's composition root — the ONE place its subscriptions are made, in order.

``printer_events`` dispatches; this module decides WHO hears WHAT, in WHICH order. It is the
injector of the ``farm_acting`` pattern for the hub: each owner keeps its own ``(printer id,
state)`` entry point, and each adapter below translates one event into one owner call, reaching the
owner through its module at call time. ``main.lifespan`` calls :func:`wire` once, before any printer
connects; nothing else calls ``printer_events.subscribe`` (``test_printer_events``).

The order per kind — the order the hand-written fan-out in ``main.on_printer_status_change`` had:

* :class:`~printer_events.HmsEdgesAppeared` — the runout spent stamps
  (``spool_respool.apply_runout_edges``, spawned: a weakly-held stamp task can be collected
  mid-await), the USB storage-low sweep (``usb_storage.on_storage_low``, spawned: a long FTPS
  sweep), and the runout guidance refresh (``spool_recovery.maybe_refresh_runout_guidance``,
  awaited: guidance only, never the escalation latch);
* :class:`~printer_events.StatusReported` — the record's reading of its outstanding actions
  (``printer_action``: the printer's answer is read before any lane acts on the same push), the
  stagger bed-ramp watch, the backup-swap sampler (an admitted switch spawns its confirmer), the
  AMS-incident wire sampler, the pause-recovery sampler — each sync, in-memory and idempotent
  through its own edge, so each rides every push — and the AMS-fault entry
  (``spool_recovery.on_ams_fault``, spawned on every push that carries an ACTIONABLE fault: a LEVEL,
  deliberately never an HMS edge — a code standing since a restart, or flapping inside notify_dedup's
  600 s window, never edges, and gating the entry on an edge made 9 runout episodes silent; the
  entry's own throttle keeps a standing fault from re-querying every second);
* :class:`~printer_events.CommandAcked`, :class:`~printer_events.FeedEventReported`,
  :class:`~printer_events.JobTerminal` — the record's reading of an ACK, a landing and a terminal
  (``printer_action``);
* :class:`~printer_events.ActionAnswered` — the AMS recovery driver's D1: the farm's queued command
  ran after the hand-over (``spool_recovery.on_action_answered``); and a commanded RFID read the record
  answered refused or void — its identify never ran, so the presence lane retires it
  (``ams_presence.on_action_answered``); and a dispatch's ``print_start`` — its claim is kept, or the
  unit returned to the queue, on that answer (``dispatch_start.on_action_answered``); and an eject
  sweep's ``eject_start`` the printer never began — its claim is retired, the plate stays gated
  (``eject.remote.on_action_answered``);
* :class:`~printer_events.FirstFreshReport` — the one session start (``session_start``);
* :class:`~printer_events.SessionLost` — the record's online clock stops counting the printer
  (``printer_action.on_session_lost``; the session start's re-read restarts it).

The record's bound factories (``printer_action.register_bound_factory``) are registered here too, at the
same one wiring point: a REHYDRATED outstanding action with no self-heal bound gets its family's at its
first evaluation, whichever of ``printer_action.rehydrate`` and :func:`wire` the lifespan ran first —
today ``print_start``'s (``dispatch_start.rehydrated_start_bound``: the dispatch's Phase A bound, counted
from the printer's first fresh report in the new process) and a runout refill's farm ``resume``
(``spool_recovery.rehydrated_runout_resume_bound``: ``_RUNOUT_RESUME_SELF_HEAL``, from its send) and a
power-loss resume (``pause_recovery.rehydrated_power_loss_resume_bound``: ``_POWER_LOSS_SELF_HEAL``, from its
send — no subscriber: the re-spawned power-loss driver's new resume holds the printer on its own bound, and the
passed bound only keeps the orphaned resume off the attention owner's page). A bounded family's lane hears
:class:`~printer_events.SelfHealDue` through a subscription made here:

* :class:`~printer_events.SelfHealDue` — an eject sweep's ``eject_start`` still unanswered past its
  start bound (``eject.remote._EJECT_START_SELF_HEAL``, 180 s online): the eject the printer never
  started is retired, the plate stays gated (``eject.remote.on_self_heal_due``); and a dispatch's
  ``print_start`` past its bound — 90 s with no answer, or 180 s after its job's id landed with no active
  word — returns the unit to the queue (``dispatch_start.on_self_heal_due``), with or without a watch alive;
  and a runout refill's farm ``resume`` past its bound (30 s online with no answer) — owed again under the
  row's resume cap, else the give-up page (``spool_recovery.on_self_heal_due``).

A dispatch's landing is read on :class:`~printer_events.StatusReported` right after the record's own
reading of the push (``dispatch_start.on_status_reported``): the start's bound is re-anchored there, once.

The stagger watch read the push first in the old fan-out and now reads it after the HMS edges'
consumers: it reads only the bed temperature, which no edge consumer touches.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from backend.app.core.tasks import spawn_background_task
from backend.app.services import (
    ams_presence,
    dispatch_start,
    hms_errors,
    pause_recovery,
    printer_action,
    printer_events,
    spool_recovery,
    spool_respool,
    stagger,
    usb_storage,
)
from backend.app.services.bambu_mqtt import AMS_STATUS_IDENTIFYING
from backend.app.services.command_outcome import Family
from backend.app.services.eject import remote as eject_remote
from backend.app.services.printer_events import (
    ActionAnswered,
    CommandAcked,
    FeedEventReported,
    FirstFreshReport,
    HmsEdgesAppeared,
    JobTerminal,
    SelfHealDue,
    SessionLost,
    StatusReported,
)
from backend.app.services.session_start import SessionStart

_unsubscribe: list[Callable[[], None]] = []


def _runout_spent_stamps(event: HmsEdgesAppeared) -> None:
    """Reused-tag re-spool Tier 1, both lanes: a runout code that APPEARED stamps ``spent_at`` on
    the exhausted tray's spool (the unrescued vocabulary and the firmware's slot-attributed
    auto-switch statement; deriving both from the appeared entries is the service's)."""
    spawn_background_task(
        spool_respool.apply_runout_edges(event.printer_id, event.edges, event.state),
        name=f"runout-edges-p{event.printer_id}",
    )


def _storage_low(event: HmsEdgesAppeared) -> None:
    """The ARRIVAL of the HMS storage-low code triggers the USB sweep; it pages failures only."""
    appeared = event.edges.appeared_full & usb_storage.HMS_STORAGE_LOW_FULL_CODES
    if appeared:
        spawn_background_task(
            usb_storage.on_storage_low(event.printer_id, set(appeared)), name=f"storage-low-p{event.printer_id}"
        )


async def _runout_guidance(event: HmsEdgesAppeared) -> None:
    """A runout demand that moved to another slot re-announces the escalation with the fresh slot
    (006-H2S 2026-07-26) — guidance only; the escalation latch is never touched."""
    await spool_recovery.maybe_refresh_runout_guidance(event.printer_id, event.edges.appeared_full, event.state)


def _stagger_bed_watch(event: StatusReported) -> None:
    """A just-started printer's bed reaching target frees its stagger slot at once (Phase E)."""
    stagger.stagger_policy.on_status_push(event.printer_id, event.state)


def _backup_swaps(event: StatusReported) -> None:
    """The firmware's AMS backup swap (the feed state's ``auto_switched``): respool's policy, once
    per switch; only an admitted switch pays for a session, under THIS push's job."""
    departed = spool_respool.sample_status_push(event.printer_id, event.state)
    if departed:
        spawn_background_task(
            spool_respool.confirm_backup_swaps(event.printer_id, departed, subtask_id=event.state.subtask_id),
            name=f"backup-swap-confirm-p{event.printer_id}",
        )


def _identify_activity(event: StatusReported) -> None:
    """The AMS unit's identify flag, sampled on EVERY status push — the edge that answers a commanded
    RFID read (``ams_presence.note_identify_activity``), whether or not the push carries tray data."""
    ams_presence.note_identify_activity(
        event.printer_id, getattr(event.state, "ams_status_main", 0) == AMS_STATUS_IDENTIFYING
    )


def _incident_wire_sampler(event: StatusReported) -> None:
    """The AMS-incident wire sampler: the refill auto-resume's spawn source, the close of a hold the
    printer runs again from, and the empty-toolhead detectors."""
    spool_recovery.note_demand_watch(event.printer_id, event.state)


def _pause_recovery_sampler(event: StatusReported) -> None:
    """The power-loss prompt's driver and the plate-check episode's trip and re-entry."""
    pause_recovery.note_status_push(event.printer_id, event.state)


def _ams_fault_entry(event: StatusReported) -> None:
    """The automatic AMS incident entry: a push whose live HMS carries an ACTIONABLE fault (the
    taxonomy's ``live_candidates``) spawns the incident machine, which opens the durable incident,
    routes by fault class and print origin and owns every notification. Per push, on the LEVEL
    (module docstring)."""
    if hms_errors.live_candidates(event.state):
        spawn_background_task(
            spool_recovery.on_ams_fault(event.printer_id, event.state), name=f"ams-fault-p{event.printer_id}"
        )


def wire(*, downtime_reconcile: Callable[[int], Awaitable[object]]) -> None:
    """Subscribe every farm lane to the hub, in order (module docstring). Idempotent: a second call
    replaces the first wiring. ``downtime_reconcile`` is ``main``'s reconcile body, injected into
    the session start (a service never imports ``main``)."""
    unwire()
    session = SessionStart(downtime_reconcile=downtime_reconcile)
    subscriptions: tuple[tuple[type[printer_events.PrinterEvent], Callable[..., object]], ...] = (
        (HmsEdgesAppeared, _runout_spent_stamps),
        (HmsEdgesAppeared, _storage_low),
        (HmsEdgesAppeared, _runout_guidance),
        (StatusReported, printer_action.on_status_reported),
        (StatusReported, dispatch_start.on_status_reported),
        (StatusReported, _stagger_bed_watch),
        (StatusReported, _backup_swaps),
        (StatusReported, _identify_activity),
        (StatusReported, _incident_wire_sampler),
        (StatusReported, _pause_recovery_sampler),
        (StatusReported, _ams_fault_entry),
        (CommandAcked, printer_action.on_command_acked),
        (FeedEventReported, printer_action.on_feed_event),
        (JobTerminal, printer_action.on_job_terminal),
        (ActionAnswered, spool_recovery.on_action_answered),
        (ActionAnswered, ams_presence.on_action_answered),
        (ActionAnswered, dispatch_start.on_action_answered),
        (ActionAnswered, eject_remote.on_action_answered),
        (SelfHealDue, eject_remote.on_self_heal_due),
        (SelfHealDue, dispatch_start.on_self_heal_due),
        (SelfHealDue, spool_recovery.on_self_heal_due),
        (FirstFreshReport, session.on_first_fresh_report),
        (SessionLost, printer_action.on_session_lost),
    )
    for kind, handler in subscriptions:
        _unsubscribe.append(printer_events.subscribe(kind, handler))
    # (command name | family, factory) — each bounded family's self-heal bound for a rehydrated action.
    bound_factories: tuple[tuple[str | Family, printer_action.BoundFactory], ...] = (
        ("print_start", dispatch_start.rehydrated_start_bound),
        ("resume", spool_recovery.rehydrated_runout_resume_bound),
        ("power_loss_resume", pause_recovery.rehydrated_power_loss_resume_bound),
    )
    for key, factory in bound_factories:
        _unsubscribe.append(printer_action.register_bound_factory(key, factory))


def unwire() -> None:
    """Remove every subscription and bound factory :func:`wire` made (shutdown, and a re-wire)."""
    while _unsubscribe:
        _unsubscribe.pop()()
