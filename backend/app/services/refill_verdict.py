"""Is a toolhead refill OWED here, and why not — the K7 verdict (2026-10-10), a LEAF.

The operator requirement this serves, verbatim (Raymond 2026-10-10 03:55 EDT): "when i click
resume there MUST be filament loaded. It's not the users job to know whether or not there's
filament in there, the fact that it's unloaded is the farms POOR auto recovery mechanics".

Its readers: the resume verb (T3), the refill driver at its publish, the restart re-entry and the
per-push detectors (T2 / T4) — all in ``spool_recovery`` — and the status frame's
``toolhead.refill_reason`` (``printer_manager.toolhead_payload``, T3: what a Resume would do about
an empty toolhead, so the card never re-derives K7). ``spool_recovery`` imports
``printer_manager``, and the status frame is built inside ``printer_manager`` too (its own
broadcast, ``_broadcast_status_change``, is maintenance mode's), so the verdict lives HERE and
reaches nothing above the client registry (``test_import_graph.TestTheRefillVerdictIsALeaf``):

* the client's peaks come in as an argument (``peaks``) — the registry's caller reads them;
* the one pure reader it composed from a module above the registry, :func:`ran` (it lived in
  ``ams_command``, which imports ``printer_manager`` to publish), lives here with the settle rule
  it feeds (:func:`pending_settles`);
* session freshness is ``live_reading``'s.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, assert_never, get_args

from backend.app.models.printer_incident import KIND_PHYSICAL, KIND_RUNOUT
from backend.app.models.printer_incident_step import STEP_KIND_COMMAND
from backend.app.schemas.printer import AmsMotionCommand
from backend.app.services import printer_incidents, tray_fields
from backend.app.services.bambu_mqtt import ams_mid_filament_change
from backend.app.services.hms_errors import current_runout_demand, power_loss_prompt_standing
from backend.app.services.live_reading import reads_live
from backend.app.services.plate_occupancy import plate_occupancy

if TYPE_CHECKING:
    from backend.app.services.bambu_mqtt import JobPeaks

# --- did a recorded motion command run -------------------------------------------------

#: The feeder-position kinds a recovery driver RECORDS at a send that name a real AMS feeder
#: at the extruder — the jammed tray, or another one (``spool_recovery.FeederKind``, written
#: on ``printer_incident_step.feeder``). Read here, never re-spelled there: the vocabulary is
#: the driver's (its module imports this one, so the reverse import would be a cycle), and
#: ``test_ams_command.TestRan`` pins this subset against it.
_RECORDED_LOADED_FEEDERS: frozenset[str] = frozenset({"jammed", "other"})

#: The motion commands a ledger step can name — the ones ``ams_command`` publishes
#: (``schemas.printer.AmsMotionCommand``, pinned equal to ``ams_command.Command``).
_COMMANDS: frozenset[str] = frozenset(get_args(AmsMotionCommand))


def ran(step: printer_incidents.StepEntry, live: tray_fields.ToolheadFeed) -> bool | None:
    """Did this RECORDED motion command run? Read off the step's own ledger columns
    (``printer_incidents.StepEntry``: the command ``name``, the load's ``target``, the
    feeder-position kind read at the send) and the LIVE toolhead feed
    (``tray_fields.toolhead_feed`` — the ACTIVE extruder). Pure and DB-free.

    Its readers ask it about a command that may still run after nobody watched it answer:
    the recovery driver's round top after a restart (never send behind a pending command)
    and the settle rule (:func:`pending_settles`) of the per-push detector and the verdict. The
    measured reason: an accepted pull-back ran ON ITS OWN ~4.5 min after the send (011-H2S
    2026-10-09 04:31:27 → 04:36:01; 014-H2S 2026-10-10 02:45:58 → 02:50:38).

    * unload — ran ⇔ the recorded entry feeder was LOADED (:data:`_RECORDED_LOADED_FEEDERS`)
      and the active extruder now reads empty. Sent with nothing loaded, or with no
      reading of what was: ``None`` (nothing physical can answer it).
    * load — ran ⇔ the active feeder now IS the target. A target that is no AMS feeder
      (the external spool, Ext-R, none): ``None`` — the feed reading cannot attribute it.
    * a live reading of ``unknown``: ``None`` (nothing was read).

    Motion-only: a step that is not a ``command``, or names a command ``ams_command`` does not
    publish, RAISES ``LookupError`` — a ledger token nobody can read back is drift, never a
    skipped row (``printer_incidents.EvidenceLog``'s rule).
    """
    if step.kind != STEP_KIND_COMMAND or step.name not in _COMMANDS:
        raise LookupError(f"refill_verdict.ran: not a motion command step (kind={step.kind!r}, name={step.name!r})")
    if live.active.kind == "unknown":
        return None
    if step.name == "unload":
        if step.feeder not in _RECORDED_LOADED_FEEDERS:
            return None
        return live.active.kind == "empty"
    target = tray_fields.valid_feeder(step.target)
    if target is None:
        return None
    return live.active.kind == "fed" and live.active.tray == target


# --- K7: is a toolhead refill OWED here? -----------------------------------------------
#
# Three EVENTS can owe a refill (the plan's triggers; "time is not the right signal", Raymond
# 2026-10-10, so none of them is a clock):
#
# * ``T2`` — a FARM command the step ledger still held as pending RAN after the hand-over (an
#   accepted pull-back runs on its own ~4.5 min after the send: 011-H2S 04:31:27 → 04:36:01,
#   014-H2S 02:45:58 → 02:50:38) — the consequence is the farm's to undo;
# * ``T3`` — a person pressed Resume in Bambuddy (the route, the HMS dialog's resume buttons) or the
#   farm resumes on a person's behalf (the refill lane, the repair self-heal) — and the status
#   frame asks what such a Resume would do;
# * ``T4`` — the first RUNNING push with nothing fed (a screen resume onto an empty toolhead, or a
#   pending pull-back that drained under a running print): Raymond 2026-09-17, "If nothing is
#   feedingg after 20secs the print is ruined already" — so the first push, never a dwell.

RefillTrigger = Literal["T2", "T3", "T4"]

# Why a refill is (not) owed — CLOSED, first match wins in :func:`refill_owed`'s order:
#
# * ``owed``               — the ACTIVE extruder reads EMPTY and nothing below holds: refill it.
# * ``fed``                — the active extruder is fed (an AMS feeder, or the external spool).
# * ``unknown``            — no live reading to decide on: the printer is not reporting on its
#                            session (``live_reading.reads_live``), there is no client to read the
#                            job's layer from (``peaks`` is ``None``), or the toolhead reading names
#                            nothing.
# * ``eject_sweep``        — the farm's own eject job owns the printer (``plate_occupancy.
#                            eject_identity``): it runs with the filament retracted by design — a
#                            sweep is filament-less, never air.
# * ``change_in_flight``   — a filament change is in flight (``bambu_mqtt.ams_mid_filament_change``):
#                            the AMS owns the toolhead's next motion — the print's own change is the
#                            head of its work, and anything sent into it is held (012-H2S 2026-09-23).
# * ``command_pending``    — a farm motion command on an open row is still QUEUED (the ledger holds it
#                            pending and the wire does not show it ran): nothing goes behind it — a
#                            second command behind a queued one runs too — and when it runs, its
#                            consequence is T2's.
# * ``power_loss_prompt``  — the firmware's post-reboot prompt stands (``0300_8007``): the reboot's own
#                            answer is the power-loss lane's (``pause_recovery``), and a ``tray_now``
#                            read across a reboot is the firmware's reset, not a measurement.
# * ``runout_demand``      — the firmware asks for the SAME slot (``hms_errors.current_runout_demand``),
#                            or the printer holds an open RUNOUT row (the demand the refill lane is
#                            answering, after it cleared off the wire): invariant 9 — a runout is a
#                            same-slot refill, never a swap, and a load during the hold LATCHES in
#                            firmware (006-H2S 2026-07-26). The refill LANE owns it.
# * ``maintenance``        — maintenance mode (``printer_incidents.automation_held``): the farm loads
#                            nothing on a printer with hands in it.
# * ``physical``           — an open PHYSICAL hold — T2 ONLY: its exit is the operator's own load, so
#                            the farm's command running after the hand-over does not reach for the
#                            path; a person's Resume (T3) and a screen resume onto air (T4) still try
#                            the refill and report a failure.
# * ``before_first_layer`` — the job has not printed layer 1 (``JobPeaks.layer_num`` through the
#                            client's one peaks reader, behind its stale-predecessor gate): the start
#                            block's own load is still to come, so a 255 there is not air (012-H2S
#                            2026-10-10: paused at layer 0 at the plate-marker dialog).
# * ``last_layer``         — T4 ONLY: a RUNNING job AT its last layer (``total_layers`` known) reads
#                            empty because of the end-of-print retract, not air — ``JobPeaks.
#                            first_unfed_layer``'s own rule (``L`` below ``total_layers``).
RefillReason = Literal[
    "owed",
    "fed",
    "maintenance",
    "physical",
    "runout_demand",
    "power_loss_prompt",
    "before_first_layer",
    "last_layer",
    "change_in_flight",
    "command_pending",
    "eject_sweep",
    "unknown",
]


@dataclass(frozen=True)
class RefillOwed:
    """:func:`refill_owed`'s verdict: the trigger it was asked for and the closed reason."""

    trigger: RefillTrigger
    reason: RefillReason

    @property
    def owed(self) -> bool:
        return self.reason == "owed"


# What settles a farm command the step ledger still holds pending, read off the live toolhead —
# ``complete`` (:func:`ran` says it ran) or ``undecidable`` (an unload sent with NOTHING loaded, or
# with no reading of what was, while the toolhead reads empty: no wire reading can say it ran, and
# its running pulls nothing back). ``None``: still queued.
PendingSettle = Literal["complete", "undecidable"]


def pending_settles(step: printer_incidents.StepEntry, live: tray_fields.ToolheadFeed) -> PendingSettle | None:
    """Has the farm's pending motion command ``step`` (its ledger columns) stopped being a queue?
    THE one rule, read by the driver's own waits (``spool_recovery._await_pending_command`` /
    ``_settle_pending``), the refill verdict (:func:`refill_owed`) and the per-push detectors
    (``spool_recovery._sample_toolhead``). Pure."""
    answer = ran(step, live)
    if answer is True:
        return "complete"
    if answer is None and step.name == "unload" and live.active.kind == "empty":
        return "undecidable"
    return None


def pending_entry(pending: printer_incidents.PendingCommand) -> printer_incidents.StepEntry:
    """The ledger columns of the open-row projection's pending command, as :func:`ran` reads a
    step."""
    return printer_incidents.StepEntry(
        kind=STEP_KIND_COMMAND, name=pending.name, target=pending.target, feeder=pending.feeder
    )


def refill_owed(printer_id: int, state, *, trigger: RefillTrigger, peaks: JobPeaks | None) -> RefillOwed:
    """Is a toolhead refill OWED on this printer now, for ``trigger``? THE one owner of the
    exclusions (K7) — no other function composes these predicates for a refill decision
    (``test_code_quality.TestResumeOwnership``).

    Pure, sync and DB-free — every fact it reads is the wire (``state``), the open-row projection
    (``printer_incidents``' cache: maintenance, the open kinds, the pending command), the plate
    authority's eject record, or the client's one peaks reader, handed in as ``peaks``
    (``BambuMQTTClient.job_peaks()``; ``None`` — no client registered: the job's layer is not a
    measurement, so nothing is decided) — because the per-push detectors and the status frame ask
    it on every status push (invariant 10). The reasons and their order are :data:`RefillReason`'s;
    the toolhead is the ACTIVE extruder's (``tray_fields.toolhead_feed``, K1 — an empty active
    nozzle beside a loaded one prints air).
    """
    if state is None or not reads_live(state):
        return RefillOwed(trigger, "unknown")
    if peaks is None:
        return RefillOwed(trigger, "unknown")
    if plate_occupancy.eject_identity(printer_id) is not None:
        return RefillOwed(trigger, "eject_sweep")
    feed = tray_fields.toolhead_feed(state)
    match feed.active.kind:
        case "fed" | "external":
            return RefillOwed(trigger, "fed")
        case "unknown":
            return RefillOwed(trigger, "unknown")
        case "empty":
            pass
        case _:
            assert_never(feed.active.kind)
    if ams_mid_filament_change(state):
        return RefillOwed(trigger, "change_in_flight")
    pending = printer_incidents.pending_command(printer_id)
    if pending is not None and pending_settles(pending_entry(pending), feed) is None:
        return RefillOwed(trigger, "command_pending")
    hms_list = getattr(state, "hms_errors", None) or []
    if power_loss_prompt_standing(hms_list):
        return RefillOwed(trigger, "power_loss_prompt")
    open_kinds = printer_incidents.open_kinds(printer_id)
    if current_runout_demand(hms_list) is not None or KIND_RUNOUT in open_kinds:
        return RefillOwed(trigger, "runout_demand")
    if printer_incidents.automation_held(printer_id):
        return RefillOwed(trigger, "maintenance")
    if trigger == "T2" and KIND_PHYSICAL in open_kinds:
        return RefillOwed(trigger, "physical")
    if peaks.layer_num < 1:
        return RefillOwed(trigger, "before_first_layer")
    total = int(getattr(state, "total_layers", 0) or 0)
    if trigger == "T4" and 0 < total <= peaks.layer_num:
        return RefillOwed(trigger, "last_layer")
    return RefillOwed(trigger, "owed")
