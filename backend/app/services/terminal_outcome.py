"""THE classification of one print terminal — built once, before any sink acts on it.

``main.on_print_complete`` used to re-derive "how did this print end" at each sink it
fed: the queue row read ``data["status"]`` after four in-place rewrites, the farm
policy read the verdict back off a DB column, the archive derived its failure reason
from whichever HMS list happened to survive, and the requeue decision asked the incident
store AFTER ``spool_recovery.on_job_terminal`` had already closed the rows it wanted to
see. Two production defects came straight out of that shape (2026-09-24):

* an operator Stop over an escalated runout / jam / power-loss hold never requeued —
  the hold was closed before the policy asked about it (the 2026-09-11 ruling, silently
  unhonoured);
* a refused plate recorded as ``failed`` would spend the plate's one retry and feed
  quarantine.

So a terminal is now classified ONCE, into the frozen :class:`TerminalOutcome`, from
facts captured BEFORE any consumer mutates state — the printer's open holds are read off
the incident store's DB-free projection ahead of every closer — and the handler threads
that one value to every sink: the queue row, the plate authority, the archive and print
log, the notification and the farm policy. This module decides; ``main`` only captures
the inputs and hands the value on (fork discipline: logic in a farm module, a small hook
in the monolith).

Pure: no DB, no I/O, no clock.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from backend.app.models.printer_incident import FAULT_KINDS, KIND_PLATE_VISION
from backend.app.services.bambu_mqtt import _HMS_PLATE_OCCUPANCY_CODES
from backend.app.services.farm_correlation import (
    OPERATOR_STOP_VERDICTS,
    STOP_SOURCE_RECONCILE_UNKNOWN,
    STOP_VERDICT_PLATE_REFUSED,
    StopVerdict,
)
from backend.app.services.hms_errors import PrinterMessage, messages_from_payload, summary_of, unique_messages
from backend.app.services.plate_occupancy import PlateRefusal

if TYPE_CHECKING:
    from backend.app.services.plate_occupancy import DepositEvidence

# The printer's own failure CATEGORIES — the archive's and print log's
# ``failure_reason``, keyed by short code. Match by full short code only: anything not in
# this map leaves the category None rather than guessing. (Earlier upstream code matched
# on ``module`` alone — "any module 0x0C HMS → Layer shift" — which mislabelled the H2D's
# own user-cancel sequence, 0C00_001B, as a layer shift.) The strings are STORED history
# that failure analysis groups by, so they are kept verbatim; a rename would split it.
#
# Moved here from ``main`` (2026-09-24): the category is one of the outcome's facts, and
# a table the builder reads cannot live in the monolith the builder is not allowed to
# import.
_HMS_FAILURE_REASONS: dict[str, str] = {
    # Layer shift / step loss
    "0300_4057": "Layer shift",
    "0300_4068": "Layer shift",
    "0300_800C": "Layer shift",
    # Filament runout (printer-side & per-AMS-slot)
    "0300_8004": "Filament runout",
    "0700_8011": "Filament runout",
    "0701_8011": "Filament runout",
    "0702_8011": "Filament runout",
    "0703_8011": "Filament runout",
    "0704_8011": "Filament runout",
    "0705_8011": "Filament runout",
    "0706_8011": "Filament runout",
    "0707_8011": "Filament runout",
    "07FF_8011": "Filament runout",
    # Clogged nozzle / extruder
    "0300_4006": "Clogged nozzle",
    "0300_8016": "Clogged nozzle",
    "0300_801C": "Clogged nozzle",
    "0700_8003": "Clogged nozzle",
    "0700_8007": "Clogged nozzle",
    "0700_8013": "Clogged nozzle",
    "0701_8003": "Clogged nozzle",
    "0701_8007": "Clogged nozzle",
    "0701_8013": "Clogged nozzle",
    "0702_8003": "Clogged nozzle",
}
# The printer's own pre-print plate check (Phase 3.3), from the one code set the wire
# layer defines, so the category and the trip lane can never name different codes.
for _occupancy_code in _HMS_PLATE_OCCUPANCY_CODES:
    _HMS_FAILURE_REASONS.setdefault(_occupancy_code, "Plate not empty (printer vision)")

# The category for a stop a HUMAN attributed and the printer explained nothing about.
USER_CANCELLED_CATEGORY = "User cancelled"

# Raw firmware words a stop can arrive as. ``aborted`` is the client's word for a job
# that ended in neither FINISH nor FAILED (``bambu_mqtt``), and the downtime reconcile's
# for an outcome it could not read.
_STOP_RAW_STATUSES: frozenset[str] = frozenset({"failed", "aborted"})


@dataclass(frozen=True, slots=True)
class TerminalOutcome:
    """How one print ended, decided once. Every sink reads THIS, never its own reading.

    ``raw_status`` — the printer's own word (``completed`` / ``failed`` / ``aborted`` /
    ``cancelled``), captured before any rewrite; the incident closers read it.

    ``recorded_status`` — what the farm RECORDS (queue row, archive, notification, farm
    policy): ``completed`` / ``failed`` / ``cancelled``, never ``aborted``. The rewrites
    that used to be scattered through ``main`` live in :func:`_recorded_status`, each
    with its reason.

    ``verdict`` — :func:`farm_correlation.classify_stop`'s closed verdict, or None.

    ``faults_open`` — the EQUIPMENT-FAULT kinds (``FAULT_KINDS``) the printer held open
    AT this terminal, read before any closer ran. "An operator stopped a print its
    printer was already holding" is decided from this captured fact, never from a store
    read after the terminal's own closers have emptied it.

    ``failure_category`` — the archive's / print log's ``failure_reason``: the printer's
    mapped category for a non-completed terminal whose evidence carries one, "User
    cancelled" for an operator-attributed stop the printer explained nothing about,
    else None.

    ``printer_message`` — the printer's own words for a non-completed terminal (the
    queue row's ``error_message``, the notification's ``{reason}``), rendered by the one
    HMS renderer; None when the printer said nothing.

    ``plate_refusal`` — set iff the verdict is ``plate_refused``: the plate authority's
    gate cause, carrying the printer's words for the check that refused the plate.
    """

    raw_status: str
    recorded_status: str
    verdict: StopVerdict | None
    faults_open: frozenset[str]
    failure_category: str | None
    printer_message: str | None
    plate_refusal: PlateRefusal | None

    @property
    def operator_stopped(self) -> bool:
        """Did a HUMAN's Stop end this print (the UI button or the printer's screen)?"""
        return self.verdict in OPERATOR_STOP_VERDICTS


def open_holds_at_terminal(open_incidents: Sequence[Mapping[str, object]]) -> frozenset[str]:
    """The equipment-fault kinds among the printer's open holds — the faults open AT the terminal."""
    return frozenset(str(incident.get("kind")) for incident in open_incidents if incident.get("kind") in FAULT_KINDS)


def _recorded_messages(incident: Mapping[str, object]) -> tuple[PrinterMessage, ...]:
    """A hold's RECORDED printer words, off its projection (``printer_messages``)."""
    raw = incident.get("printer_messages") or []
    messages: list[PrinterMessage] = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, Mapping) and entry.get("short_code"):
            messages.append(
                PrinterMessage(short_code=str(entry["short_code"]), description=str(entry.get("description") or ""))
            )
    return tuple(messages)


def _recorded_status(
    raw_status: str,
    verdict: StopVerdict | None,
    evidence: DepositEvidence,
    *,
    first_article: bool,
    is_eject: bool,
) -> str:
    """The farm's word for this terminal. Every rewrite, with its reason, in one place.

    * ``plate_refused`` → ``cancelled``, a FIRST ARTICLE included. The printer refused
      the plate; nothing was printed and nothing failed. Recorded ``failed``, a first
      article would spend its one retry (``requeue.failed_ancestor_count``) and feed
      quarantine for a plate the printer itself turned away.
    * an operator's UI Stop → ``cancelled`` (an explicit queue action, whatever the
      firmware called it).
    * a job that deposited NOTHING and is neither a first article nor an eject →
      ``cancelled``: it cannot have fouled the bed and is not a print failure. A first
      article keeps ``failed`` so a genuine FA failure still retries; an eject sweep's
      word is the eject lane's own.
    * a screen Stop that DID deposit → ``cancelled``: a stop, not a failure.
    * the downtime reconcile's unknown outcome → ``cancelled``: nothing was observed to
      fail, so it must not feed retry or quarantine accounting.
    * ANY other ``aborted`` → ``cancelled``. ``aborted`` is the client's word for a job
      that ended in neither FINISH nor FAILED, and nobody attributed it — and an outcome
      nobody can attribute is not a completed one (2026-09-19). Left ``aborted``, the
      farm policy matched no branch of its disposition fork and the run ended one plate
      short in silence; as ``cancelled`` it takes the operator-stop disposition (the run
      holds, a human is paged, RESUME tops the deficit back up), and ONE word describes
      the terminal in the queue row, the archive and the page alike.
    """
    if verdict == STOP_VERDICT_PLATE_REFUSED and raw_status != "completed":
        return "cancelled"
    if verdict == "operator_ui" and raw_status in _STOP_RAW_STATUSES:
        return "cancelled"
    if not evidence.deposited and not first_article and not is_eject:
        return "cancelled"
    if verdict == "operator_screen" and evidence.deposited and raw_status in _STOP_RAW_STATUSES:
        return "cancelled"
    if verdict == STOP_SOURCE_RECONCILE_UNKNOWN and raw_status in _STOP_RAW_STATUSES:
        return "cancelled"
    if raw_status == "aborted":
        return "cancelled"
    return raw_status


def build_terminal_outcome(
    *,
    raw_status: str,
    verdict: StopVerdict | None,
    open_incidents: Sequence[Mapping[str, object]],
    job_id: str | None,
    evidence: DepositEvidence,
    first_article: bool,
    is_eject: bool,
    hms_errors: list[dict] | None,
) -> TerminalOutcome:
    """Build THE outcome of one terminal from facts the caller captured before any sink acted.

    ``open_incidents`` must be the SAME projection ``verdict`` was classified over (read
    once, ahead of every closer). ``job_id`` is the terminal's echoed ``subtask_id``:
    the printer's recorded words for a hold count as this terminal's evidence only when
    the hold paused THIS job — a hold from another job explains nothing about this one.

    The evidence the category and the message are built from is the holds' recorded
    words first (they are WHY the job was held, and a stop wipes the printer's live list,
    so a refused plate's codes survive only there) and then the terminal payload's own
    HMS list, de-duplicated by short code.
    """
    job = (job_id or "").strip()
    this_job = [incident for incident in open_incidents if str(incident.get("job_id") or "").strip() == job]
    recorded = [message for incident in this_job for message in _recorded_messages(incident)]
    printer_evidence = unique_messages([*recorded, *messages_from_payload(hms_errors)])

    recorded_status = _recorded_status(raw_status, verdict, evidence, first_article=first_article, is_eject=is_eject)
    ended_without_part = recorded_status != "completed"

    category: str | None = None
    if ended_without_part:
        category = next(
            (_HMS_FAILURE_REASONS[m.short_code] for m in printer_evidence if m.short_code in _HMS_FAILURE_REASONS),
            None,
        )
        if category is None and verdict in OPERATOR_STOP_VERDICTS:
            category = USER_CANCELLED_CATEGORY

    refusal: PlateRefusal | None = None
    if verdict == STOP_VERDICT_PLATE_REFUSED:
        refusal = PlateRefusal(
            messages=unique_messages(
                [
                    message
                    for incident in this_job
                    if incident.get("kind") == KIND_PLATE_VISION
                    for message in _recorded_messages(incident)
                ]
            )
        )

    return TerminalOutcome(
        raw_status=raw_status,
        recorded_status=recorded_status,
        verdict=verdict,
        faults_open=open_holds_at_terminal(open_incidents),
        failure_category=category,
        printer_message=summary_of(printer_evidence) if ended_without_part else None,
        plate_refusal=refusal,
    )
