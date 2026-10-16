"""THE printer card's hold chip — which hold it names, in which tone, with which words (2026-10-11).

Before it the verdict was split: the backend projected the open rows, the toolhead feed and the
farm's refill, and the frontend (``utils/incidentChip.ts``) ranked them, picked the tone and keyed the
copy. The chip read the ROW's label, so a ``toolhead_refill`` row escalated by a build that misread the
firmware's own backup switch showed red "Toolhead empty" all night over a toolhead the wire reported
FED (001-H2S incident 574) — a stored label standing in for a live fact. ONE builder now decides the
whole verdict from the live facts and the record, and the frontend renders it (``components/HoldChip``).

ONE order, first match wins:

1. **The farm's refill in flight or failed** (``printer_incidents.refill_state``, read only while the
   active extruder reads EMPTY or UNREAD) → "Toolhead empty": amber while the farm loads (the slot),
   red once a load or unload did not reach its end (the step, the AMS's answer, the exit).
2. **The open incident row** the store ranks first (``printer_incidents.snapshot``; a kind with its own
   surface — maintenance mode — is skipped):
   * an ESCALATED row carrying the farm's stop of a print on AIR (``printer_incidents.PAYLOAD_AIR_STOP``)
     whose last farm stop was neither reached nor taken over — the printer did not take it, the driver's
     ``air_stop_not_taken`` give-up → red "Air stop not taken": what happened, the printer's words, "Stop
     the print on the printer.";
   * the resume states, ONLY on a kind the farm itself resumes (:data:`RESUME_KINDS`: the toolhead refill,
     the runout refill, the repair class) and ONLY with no recovery driver live
     (``printer_incidents.driver_live``) — a live driver's resume is one rung of its own work, so a jam
     the driver is working reads "Recovering" below (R29, 2026-10-12; production showed "Resuming" over
     a live jam):
     - the farm's last resume REFUSED by the printer, the print PAUSEd: red "Resume refused" (the
       printer's words, "Press Resume.") only once ``incident_resolution.resume_spent`` says the farm GAVE
       UP on the row; while a resume is still owed (a runout row's refill resume is re-owed up to
       ``RUNOUT_RESUME_CAP``) amber "Resuming": the printer's words, "the farm resends it";
     - the farm's resume OUTSTANDING → amber "Resuming": the feeding slot, when it went out;
   * a ``toolhead_refill`` row: a FED toolhead is never "Toolhead empty" — amber "Resuming" while the
     print runs, its refill episode still holds the row, or the spawner's own verdict
     (``incident_resolution.resume_owed``) is ``owed`` or ``waiting`` (a guard the farm waits out: its
     AMS command pending, the path not quiet); ``declined`` because another open row owns the pause
     (the plate check, a runout, an AMS hold) → that row's chip; ``declined`` otherwise (the once
     spent, maintenance mode) → red "Toolhead fed" + "Press Resume."; an empty one is "Toolhead
     empty", amber while the farm acts, red with the exit once it is a person's;
   * any other row: its kind, amber "Recovering" while the farm acts, red once escalated.
3. **A paused print on an empty toolhead** with nothing open → red "Toolhead empty", only for a T3
   verdict a person should see (``refill_verdict.refill_owed``): what a Resume would do.

The payload is keys and interpolation values (i18n keys, every locale), never sentences — JSON
primitives only: the WS lane dumps it bare. Pure, DB-free and total: it runs on every status broadcast
(``printer_manager.printer_state_to_dict``) and both REST ``/status`` branches. A LEAF of the recovery
graph: it reads the store's projection, the rule table and the verdicts, never the driver.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from backend.app.models.printer_incident import (
    KIND_PHYSICAL,
    KIND_RUNOUT,
    KIND_SERVICE_HOLD,
    KIND_TOOLHEAD_REFILL,
    STATUS_ESCALATED,
)
from backend.app.services import incident_resolution, printer_incidents, refill_verdict
from backend.app.services.command_outcome import Answer
from backend.app.services.feed_state import FeedReading
from backend.app.services.tray_fields import TRAY_NOW_EXTERNAL_SPOOL, toolhead_feed
from backend.app.utils import site_time

if TYPE_CHECKING:
    from backend.app.services.bambu_mqtt import PrinterState

ChipTone = Literal["acting", "held"]
ChipVariant = Literal["incident", "toolhead"]

#: Kinds whose surface is their own (the maintenance banner): never a chip.
OWN_SURFACE_KINDS: frozenset[str] = frozenset({KIND_SERVICE_HOLD})

#: Kinds whose ``incidentAction`` names a PERSON's exits that do not exist while the farm still acts, so
#: the chip shows ``printers.incidentRecoveringAction.<kind>`` until the row is escalated. ``plate_vision``:
#: the farm re-checks and stop-and-retries first ("Ignore and resume" appears only on the person's turn).
#: ``toolhead_refill``: the farm refills and resumes on its own. Every other kind keeps its one
#: instruction in both states (a ``runout`` row is ``recovering`` while it waits for the refill, so its
#: instruction is what the operator needs). ``__tests__/i18n/incidentKinds.test.ts`` pins the locale leaves.
RECOVERING_ACTION_KINDS: frozenset[str] = frozenset({"plate_vision", KIND_TOOLHEAD_REFILL})

#: Kinds whose hold the farm ends by RESUMING the paused print outside a recovery driver, so the chip's
#: "Resuming" / "Resume refused" speak for them: the toolhead refill's resume, the runout refill's resume
#: (invariant 9) and the repair class's resume after a hand repair (``physical`` — the one kind of the
#: ``repair`` class; its external-holder variant is a wire hold the farm never resumes, so it carries no
#: farm resume to read). A jam's resume is a rung of its driver, read as "Recovering" while it lives.
RESUME_KINDS: frozenset[str] = frozenset({KIND_TOOLHEAD_REFILL, KIND_RUNOUT, KIND_PHYSICAL})

# The T3 verdicts a person should see on a paused print over an empty toolhead with nothing open, and
# their tooltip. Every other verdict is the printer's own business (the start block loads, a runout
# demand asks for the same slot, the reboot prompt, the end retract, a change in flight, a sweep) or
# says nothing is wrong (``fed``) or nothing is known (``unknown``, ``unconfirmed``): no chip.
_PAUSED_EMPTY_TOOLTIP: Mapping[str, str] = {
    "owed": "printers.toolhead.reason.owed",
    "maintenance": "printers.toolhead.reason.maintenance",
    "physical": "printers.toolhead.reason.physical",
    "command_pending": "printers.toolhead.reason.command_pending",
}

# A failed refill's sentences per failed STEP: the lead (with / without the slot) and what the AMS
# answered. ``acted`` names the step, so an unload that stopped part-way never reads as a load.
_FAILED_COPY: Mapping[str, tuple[str, str, Mapping[str, str]]] = {
    "load": (
        "printers.toolhead.loadFailed",
        "printers.toolhead.loadFailedAnySlot",
        {"no_movement": "printers.toolhead.answer.no_movement", "acted": "printers.toolhead.answer.loadActed"},
    ),
    "unload": (
        "printers.toolhead.unloadFailed",
        "printers.toolhead.unloadFailedAnySlot",
        {"no_movement": "printers.toolhead.answer.no_movement", "acted": "printers.toolhead.answer.unloadActed"},
    ),
}

_TOOLHEAD_EMPTY = "printers.incident.toolhead_refill"
_TOOLHEAD_EXIT = "printers.incidentAction.toolhead_refill"
_RESUMING = "printers.toolhead.label.resuming"
_RESUME_REFUSED = "printers.toolhead.label.resumeRefused"
_TOOLHEAD_FED = "printers.toolhead.label.fed"
_PRESS_RESUME = "printers.toolhead.pressResume"
_REFUSED_BY = "printers.toolhead.refusedBy"
_RESUME_RESENT = "printers.toolhead.resumeResent"
_RECOVERING = "printers.incident.recovering"
_AIR_STOP_NOT_TAKEN = "printers.toolhead.label.airStopNotTaken"
_AIR_STOP_LEAD = "printers.toolhead.airStopNotTaken"
_STOP_ON_PRINTER = "printers.toolhead.stopOnPrinter"

# The record's answers (``command_outcome.Answer``) under which a farm stop was TAKEN: the job ended by
# the farm's hand, or a person's command took the printer. Any other last stop on an escalated air-stop
# row is the one the printer did not take.
_STOP_TAKEN: frozenset[str] = frozenset({Answer.REACHED.value, Answer.TAKEN_OVER.value})


@dataclass(frozen=True)
class ChipCopy:
    """One translatable sentence: an i18n key and its interpolation values."""

    key: str
    values: Mapping[str, str] = field(default_factory=dict)

    def as_payload(self) -> dict:
        return {"key": self.key, "values": dict(self.values)}


@dataclass(frozen=True)
class HoldChip:
    """The chip as the card renders it: the pill's noun, the tone, the tooltip's sentences in reading
    order (supplementary detail — the tooltip, never inline), and a raw qualifier the tooltip ends with
    (the hold's slot), or ``None``."""

    variant: ChipVariant
    tone: ChipTone
    label: ChipCopy
    tooltip: tuple[ChipCopy, ...]
    qualifier: str | None = None

    def as_payload(self) -> dict:
        return {
            "variant": self.variant,
            "tone": self.tone,
            "label": self.label.as_payload(),
            "tooltip": [copy.as_payload() for copy in self.tooltip],
            "qualifier": self.qualifier,
        }


@dataclass(frozen=True)
class _CachedRow:
    """The store's cached row as the rule table's :class:`incident_resolution.HoldRow` reads it."""

    id: int
    printer_id: int
    kind: str
    code: str
    job_id: str | None


def hold_chip_payload(printer_id: int | None, state: PrinterState | None, feed: FeedReading | None) -> dict | None:
    """THE chip this printer shows, as a JSON-primitive dict, or ``None`` (module docstring). ``feed`` is
    the client's feed state (``BambuMQTTClient.feed()``; ``None`` with no client)."""
    chip = _hold_chip(printer_id, state, feed)
    return chip.as_payload() if chip is not None else None


def _hold_chip(printer_id: int | None, state: PrinterState | None, feed: FeedReading | None) -> HoldChip | None:
    if not printer_id:
        return None
    active = toolhead_feed(state).active
    fed = active.kind in ("fed", "external") or (feed is not None and feed.toolhead_fed)
    if not fed and active.kind in ("empty", "unknown"):
        refill = printer_incidents.refill_state(printer_id)
        if refill is not None:
            return _refill_chip(refill)
    row = printer_incidents.snapshot(printer_id)
    if row is not None and row.get("kind") not in OWN_SURFACE_KINDS:
        return _row_chip(printer_id, row, state, feed, fed=fed)
    if _live_word(state) == "PAUSE" and active.kind == "empty":
        reason = refill_verdict.refill_owed(printer_id, state, trigger="T3", reading=feed).reason
        key = _PAUSED_EMPTY_TOOLTIP.get(reason)
        if key is not None:
            return HoldChip("toolhead", "held", ChipCopy(_TOOLHEAD_EMPTY), (ChipCopy(key),))
    return None


def _refill_chip(refill: Mapping[str, object]) -> HoldChip:
    """Rule 1: the farm's refill — loading (amber) or a step that did not reach its end (red)."""
    slot = refill.get("slot") if isinstance(refill.get("slot"), str) else None
    if refill.get("phase") == "loading":
        lead = (
            ChipCopy("printers.toolhead.loading", {"slot": slot})
            if slot
            else ChipCopy("printers.toolhead.loadingAnySlot")
        )
        return HoldChip("toolhead", "acting", ChipCopy(_TOOLHEAD_EMPTY), (lead,))
    command = refill.get("command") if refill.get("command") in _FAILED_COPY else "load"
    with_slot, any_slot, answers = _FAILED_COPY[str(command)]
    sentences = [ChipCopy(with_slot, {"slot": slot}) if slot else ChipCopy(any_slot)]
    answer = refill.get("answer")
    if isinstance(answer, str) and answer in answers:
        sentences.append(ChipCopy(answers[answer]))
    sentences.append(ChipCopy(_TOOLHEAD_EXIT))
    return HoldChip("toolhead", "held", ChipCopy(_TOOLHEAD_EMPTY), tuple(sentences))


def _row_chip(
    printer_id: int, row: Mapping[str, object], state: PrinterState | None, feed: FeedReading | None, *, fed: bool
) -> HoldChip:
    """Rule 2: the open row the store ranks first."""
    kind = str(row.get("kind") or "")
    incident_id = row.get("id")
    qualifier = row.get("slot_desc") if isinstance(row.get("slot_desc"), str) else None
    live = _live_word(state)
    escalated = row.get("status") == STATUS_ESCALATED
    feeding = _feeding(state, feed) if fed else ()
    if escalated and isinstance(incident_id, int) and row.get(printer_incidents.PAYLOAD_AIR_STOP):
        stop = printer_incidents.last_farm_action(printer_id, incident_id, "stop")
        if stop is not None and stop.answer not in _STOP_TAKEN:
            words = (ChipCopy(_REFUSED_BY, {"reason": stop.reason}),) if stop.reason else ()
            lead = (ChipCopy(_AIR_STOP_LEAD), *words, ChipCopy(_STOP_ON_PRINTER))
            return HoldChip("incident", "held", ChipCopy(_AIR_STOP_NOT_TAKEN), lead, qualifier)
    if kind in RESUME_KINDS and isinstance(incident_id, int) and not printer_incidents.driver_live(printer_id):
        chip = _resume_chip(printer_id, incident_id, live, feeding, qualifier)
        if chip is not None:
            return chip
    if kind == KIND_TOOLHEAD_REFILL:
        if fed:
            # The farm resumes it: the print already runs, or the farm's own episode still holds the row
            # (``recovering`` — its resume is next).
            if live == "RUNNING" or not escalated or not isinstance(incident_id, int):
                return HoldChip("toolhead", "acting", ChipCopy(_RESUMING), feeding, qualifier)
            # Otherwise the spawner's own verdict decides: owed, or a guard the farm itself waits out →
            # "Resuming"; another open row owns the pause → that row's chip; the farm will never resume
            # it → a person's Resume.
            verdict = _resume_verdict(printer_id, row, state, feed)
            if verdict.verdict in ("owed", "waiting"):
                return HoldChip("toolhead", "acting", ChipCopy(_RESUMING), feeding, qualifier)
            owner = printer_incidents.snapshot(printer_id, kind=verdict.owner) if verdict.owner else None
            if owner is not None and owner.get("kind") != KIND_TOOLHEAD_REFILL:
                return _row_chip(printer_id, owner, state, feed, fed=fed)
            return HoldChip("toolhead", "held", ChipCopy(_TOOLHEAD_FED), (*feeding, ChipCopy(_PRESS_RESUME)), qualifier)
        tooltip = ChipCopy(_TOOLHEAD_EXIT if escalated else "printers.incidentRecoveringAction.toolhead_refill")
        return HoldChip("toolhead", "held" if escalated else "acting", ChipCopy(_TOOLHEAD_EMPTY), (tooltip,), qualifier)
    action = (
        f"printers.incidentRecoveringAction.{kind}"
        if not escalated and kind in RECOVERING_ACTION_KINDS
        else f"printers.incidentAction.{kind}"
    )
    label = ChipCopy(f"printers.incident.{kind}" if escalated else _RECOVERING)
    return HoldChip("incident", "held" if escalated else "acting", label, (ChipCopy(action),), qualifier)


def _resume_chip(
    printer_id: int, incident_id: int, live: str, feeding: tuple[ChipCopy, ...], qualifier: str | None
) -> HoldChip | None:
    """The farm's resume on a :data:`RESUME_KINDS` row with no driver live, or ``None`` when its record says
    nothing the chip names. A resume the printer REFUSED over a PAUSEd print is "Resume refused" only once
    the row's budget is spent and the farm gave up (``incident_resolution.resume_spent`` — THE decider, never
    a cap re-derived here); while the row is still owed one (a runout refill resume, re-owed after a refusal)
    it reads "Resuming", so the chip never flashes red between a refusal and its resend. An OUTSTANDING
    resume reads "Resuming" with when it went out."""
    resume = printer_incidents.last_farm_action(printer_id, incident_id, "resume")
    if resume is None:
        return None
    if resume.printer_refused and live == "PAUSE":
        words = (ChipCopy(_REFUSED_BY, {"reason": resume.reason}),) if resume.reason else ()
        spent = incident_resolution.resume_spent(printer_id, incident_id)
        if spent is None:
            return HoldChip("incident", "acting", ChipCopy(_RESUMING), (*words, ChipCopy(_RESUME_RESENT)), qualifier)
        if spent.gave_up:
            return HoldChip("incident", "held", ChipCopy(_RESUME_REFUSED), (*words, ChipCopy(_PRESS_RESUME)), qualifier)
        return None
    if resume.outstanding:
        return HoldChip("incident", "acting", ChipCopy(_RESUMING), (*feeding, *_sent_at(resume.sent_at)), qualifier)
    return None


def _resume_verdict(
    printer_id: int, row: Mapping[str, object], state: PrinterState | None, feed: FeedReading | None
) -> incident_resolution.ResumeOwed:
    """The spawner's own question (``incident_resolution.resume_owed``) asked of the cached row, so
    "Resuming" is shown exactly while the farm will resume it (owed, or waiting out its own guard)."""
    incident_id = row["id"]
    assert isinstance(incident_id, int)
    view = _CachedRow(
        id=incident_id,
        printer_id=printer_id,
        kind=str(row.get("kind") or ""),
        code=str(row.get("code") or ""),
        job_id=str(row.get("job_id") or "") or None,
    )
    ctx = incident_resolution.Context(
        state=state,
        driver_live=printer_incidents.driver_live(printer_id),
        command_pending=printer_incidents.pending_command(printer_id, incident_id=incident_id) is not None,
        feed=feed,
    )
    return incident_resolution.resume_owed(view, ctx)


def _feeding(state: PrinterState | None, feed: FeedReading | None) -> tuple[ChipCopy, ...]:
    """ "Toolhead fed from AMS A slot 2." — the feed state's effective feeder (a drained roll's TAIL is
    still the draining roll), else the frame's; the external holder by name; nothing nameable → none."""
    if feed is not None and feed.feeder is not None:
        tray: int | None = feed.feeder
    else:
        active = toolhead_feed(state).active
        tray = TRAY_NOW_EXTERNAL_SPOOL if active.kind == "external" else active.tray
    if tray == TRAY_NOW_EXTERNAL_SPOOL:
        return (ChipCopy("printers.toolhead.fedFromExternal"),)
    slot = printer_incidents.runout_slot_desc(tray)
    return (ChipCopy("printers.toolhead.fedFrom", {"slot": slot}),) if slot else ()


def _sent_at(sent_at: datetime | None) -> tuple[ChipCopy, ...]:
    """ "Resume sent 23:43." in the site's clock (the record's ``sent_at`` is naive UTC)."""
    if sent_at is None:
        return ()
    return (ChipCopy("printers.toolhead.resumeSent", {"time": site_time.to_site(sent_at).strftime("%H:%M")}),)


def _live_word(state: PrinterState | None) -> str:
    return str(getattr(state, "state", None) or "").upper() if state is not None else ""
