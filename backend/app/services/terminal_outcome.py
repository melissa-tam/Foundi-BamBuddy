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
log, the filament charge, the notification and the farm policy. This module decides; ``main`` only captures
the inputs and hands the value on (fork discipline: logic in a farm module, a small hook
in the monolith).

Pure: no DB, no I/O, no clock (one WARNING line aside, :func:`build_terminal_outcome`).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args

from backend.app.models.printer_incident import FAULT_KINDS, KIND_PLATE_VISION
from backend.app.services.farm_correlation import (
    OPERATOR_STOP_VERDICTS,
    REQUEUE_VERDICTS,
    STOP_SOURCE_RECONCILE_UNKNOWN,
    STOP_VERDICT_PLATE_REFUSED,
    StopVerdict,
)
from backend.app.services.hms_errors import (
    PLATE_CHECK_HMS_CODES,
    PrinterMessage,
    messages_from_payload,
    summary_of,
    unique_messages,
)
from backend.app.services.job_identity import is_held_job
from backend.app.services.plate_occupancy import PlateRefusal

if TYPE_CHECKING:
    from backend.app.services.plate_occupancy import DepositEvidence

logger = logging.getLogger(__name__)

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
# The printer's own pre-print plate check, from the one code set the HMS taxonomy
# defines, so the category and the trip lane can never name different codes.
for _occupancy_code in PLATE_CHECK_HMS_CODES:
    _HMS_FAILURE_REASONS.setdefault(_occupancy_code, "Plate not empty (printer vision)")

# The category for a stop a HUMAN attributed and the printer explained nothing about.
USER_CANCELLED_CATEGORY = "User cancelled"

# The category of a FINISH the farm records ``failed`` because the job printed layers with NOTHING
# fed (:func:`_printed_without_filament`). A KEY, not an English label like the printer's categories
# above: the archive's and print log's ``failure_reason`` renders through the frontend's
# ``editArchive.failureReasons`` copy, with the stored value as its own fallback (which is how the
# legacy English categories still render), so this one is worded once per locale there. The
# layer-specific sentence is the outcome's ``printer_message`` (:func:`_printed_without_filament_sentence`).
PRINTED_WITHOUT_FILAMENT_CATEGORY = "printed_without_filament"

# Raw firmware words a stop can arrive as. ``aborted`` is the client's word for a job
# that ended in neither FINISH nor FAILED (``bambu_mqtt``), and the downtime reconcile's
# for an outcome it could not read.
_STOP_RAW_STATUSES: frozenset[str] = frozenset({"failed", "aborted"})

# What the job that ended is charged for — :func:`_charge_basis` decides it, the usage tracker
# obeys it. ``full`` = the whole plate's slicer grams; ``partial`` = to the extent the TERMINAL
# PAYLOAD says the job fed (``job_extent.JobExtent.charged_extent``: its own peaks, ending at the
# first layer it printed with nothing fed — never the live printer's, which by then may describe
# another job); ``none`` = nothing is charged.
ChargeBasis = Literal["full", "partial", "none"]

# How far back the plate-check ladder looks for the farm's OTHER failed-re-check stops on the same
# printer (operator ruling 2026-09-29: "if the retry's re-check fails too, escalate"). Read at the
# ladder's second rung, where the retry-or-escalate decision is made ONCE (``pause_recovery``),
# never at the terminal. Derived on 2026-09-04 (the first self-heal lane's
# ``farm_policy._VISION_RECHECK_WINDOW_S``, recovered from b85a5f25^) from what the two ends of the
# window have to separate: a retry re-dispatches within minutes (the requeued unit takes the next
# scheduler tick, and its own start G-code IS the re-check), so an hour comfortably contains two
# genuine consecutive failures; and an unrelated trip a shift later must NOT count as the second,
# because "twice in a row" is the whole evidence for calling a plate occupied.
PLATE_RECHECK_WINDOW_S = 3600.0

# The plate-check ladder's stop-step NAMES on the episode's ledger. The retry-or-escalate decision is
# made ONCE, at the ladder's second rung (``pause_recovery``), and RECORDED as the name of the stop
# the farm sends (operator ruling 2026-10-05): ``retry_stop`` — the printer's first farm stop in the
# window, on a farm unit, with nothing deposited: the same print is retried; ``deadline_stop`` — the
# decision hold's window ran out with nobody pressing Ignore and resume or Stop: the plate is a
# human's. The terminal reads the name back (:func:`farm_retries`) and never re-decides, so a stop
# whose terminal lands after the window has aged cannot change its meaning. Declared here, beside
# their one reader, because the writer (``pause_recovery``) imports this module.
PlateCheckStopName = Literal["retry_stop", "deadline_stop"]
PLATE_CHECK_RETRY_STOP: PlateCheckStopName = "retry_stop"
PLATE_CHECK_DEADLINE_STOP: PlateCheckStopName = "deadline_stop"
PLATE_CHECK_STOP_NAMES: frozenset[str] = frozenset(get_args(PlateCheckStopName))

# What the READ of a plate-check stop answered when the stop did NOT end the job: ``not_taken`` —
# the job was still PAUSEd when the confirm budget ran out; ``not_sent`` — the raw stop never went
# out. Either way the driver handed the paused print to a human (the FALLBACK), so a later end of
# that job is the human's, never the farm's stop (:attr:`PlateCheckFacts.farm_stopped`).
PlateCheckStopNotTaken = Literal["not_taken", "not_sent"]
PLATE_CHECK_STOP_NOT_TAKEN: frozenset[str] = frozenset(get_args(PlateCheckStopNotTaken))


@dataclass(frozen=True, slots=True)
class PlateCheckFacts:
    """What the plate-check episode of the job that ended says about its terminal.

    Read by ``pause_recovery.plate_check_facts`` BEFORE any closer runs (the episode's row is
    one of the holds that terminal closes), and ``None`` there when the job had no plate-check
    episode at all.

    ``last_stop`` — the LAST farm stop on the episode's ledger as ``(name, outcome)``: its name
    is the decision the ladder recorded when it sent it (:data:`PLATE_CHECK_RETRY_STOP` /
    :data:`PLATE_CHECK_DEADLINE_STOP`), its outcome what the read of it answered (``None`` while
    unanswered). ``None`` when the farm sent no stop — an operator's own Stop, or a job the
    printer ended by itself.

    ``item_id`` — the farm unit the episode's ROW bound at its trip (by job identity, farm units
    only; ``None`` for a foreign print). The row's binding, not the terminal's correlation
    verdict: the decision was made over this row, so the retry it recorded is this unit's.
    """

    last_stop: tuple[str, str | None] | None
    item_id: int | None

    @property
    def farm_stopped(self) -> bool:
        """Did the FARM's last stop end this job? True for a stop answered ``taken`` OR still
        unanswered (the job's terminal usually lands while the driver is still watching for its
        answer); False for one answered as NOT having ended the job
        (:data:`PlateCheckStopNotTaken` — the driver then handed the paused print to a human),
        and with no farm stop at all."""
        return self.last_stop is not None and self.last_stop[1] not in PLATE_CHECK_STOP_NOT_TAKEN


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

    ``failure_category`` — the archive's / print log's ``failure_reason``:
    :data:`PRINTED_WITHOUT_FILAMENT_CATEGORY` for a FINISH recorded failed because the job
    printed on air, else the printer's mapped category for a non-completed terminal whose
    evidence carries one, "User cancelled" for an operator-attributed stop the printer
    explained nothing about, else None.

    ``printer_message`` — why a non-completed terminal ended, for the operator (the queue
    row's ``error_message``, the notification's ``{reason}``): the farm's measured sentence
    for a FINISH that printed without filament first ("Printed without filament from layer
    93 of 167"), then the printer's own words, rendered by the one HMS renderer; None when
    neither has anything to say.

    ``plate_refusal`` — set when the verdict is ``plate_refused`` and the refusal is
    ESCALATED: the plate authority's gate cause, carrying the printer's words for the check
    that refused the plate. ``None`` on a ``plate_refused`` verdict the farm RETRIES
    (:func:`farm_retries`) — the verdict still records the unit ``cancelled`` and requeues
    it, but no gate rises and nobody is paged.

    ``charge`` — the filament charge basis (:data:`ChargeBasis`), decided once by
    :func:`_charge_basis` from the terminal's own evidence.
    """

    raw_status: str
    recorded_status: str
    verdict: StopVerdict | None
    faults_open: frozenset[str]
    failure_category: str | None
    printer_message: str | None
    plate_refusal: PlateRefusal | None
    charge: ChargeBasis

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


def _printed_without_filament(raw_status: str, evidence: DepositEvidence, *, is_eject: bool) -> bool:
    """Did the printer say FINISH over layers it printed with NOTHING fed (``JobExtent.printed_unfed``)?

    The FINISH is the firmware reaching the end of its G-code, not filament reaching the plate: a job
    resumed onto an empty toolhead runs to the end on air (011-H2S from layer 93 of 167, 014-H2S from
    layer 9, 2026-10-09; the firmware does not reload on resume). An eject sweep and a dry run are
    motion-only files, which feed nothing by design, so an empty extruder under them proves nothing.
    """
    return raw_status == "completed" and evidence.printed_unfed and not is_eject and not evidence.is_dry_run


def _printed_without_filament_sentence(evidence: DepositEvidence) -> str:
    """The operator's sentence for :func:`_printed_without_filament` — the measured layer, and the
    job's layer count when the slicer stated one."""
    total = f" of {evidence.total_layers}" if evidence.total_layers else ""
    return f"Printed without filament from layer {evidence.first_unfed_layer}{total}"


def _recorded_status(
    raw_status: str,
    verdict: StopVerdict | None,
    evidence: DepositEvidence,
    *,
    first_article: bool,
    is_eject: bool,
) -> str:
    """The farm's word for this terminal. Every rewrite, with its reason, in one place.

    * a FINISH that printed layers with nothing fed (:func:`_printed_without_filament`) → ``failed``,
      a first article included: the part was not made past the unfed layer, so it is not a produced
      unit. It takes the ordinary failed disposition — the plate's one retry, the printer's
      consecutive-failure count, the plate gate of a deposit — instead of completing a unit and
      charging the whole plate (011-H2S / 014-H2S, 2026-10-09). The only rewrite of a FINISH, so
      first; an eject sweep and a dry run are excluded (motion-only).
    * a requeue verdict (``farm_correlation.REQUEUE_VERDICTS``) → ``cancelled``, a FIRST
      ARTICLE included. ``plate_refused``: the printer refused the plate; nothing was
      printed and nothing failed. ``fault_restart``: the farm stopped a job that had
      deposited nothing, over a feed stall on its first filament load, to restart it on
      the backup spool (operator ruling 2026-09-29). Recorded ``failed``, a first article
      would spend its one retry (``requeue.failed_ancestor_count``) and feed quarantine
      for a plate the printer turned away or the farm itself chose to restart.
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
    if _printed_without_filament(raw_status, evidence, is_eject=is_eject):
        return "failed"
    if verdict in REQUEUE_VERDICTS and raw_status != "completed":
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


def _charge_basis(raw_status: str, verdict: StopVerdict | None, evidence: DepositEvidence) -> ChargeBasis:
    """What the job that ended is charged for — from THIS terminal's evidence, never the live printer.

    * the downtime reconcile's unknown outcome (verdict ``reconcile_unknown``, the payload's
      ``outcome_unknown``) → ``none``: nobody observed how the job ended or how far it ran.
    * the job printed a layer with NOTHING fed (a measured ``first_unfed_layer``) → ``partial``, to
      the extent it fed (``JobExtent.charged_extent``) — a FINISH included, and after an attach too
      (the layer number is absolute). A layer printed on air is not a layer fed: 011-H2S and 014-H2S
      (2026-10-09) were resumed onto an empty toolhead, ran to the end on air, said FINISH and were
      charged the whole plate.
    * the printer said FINISH (raw ``completed``) → ``full``: the whole plate ran through. The RAW
      word, because it is the printer's own statement that the job reached its end; a recorded
      rewrite (a dry run recorded ``cancelled``) does not un-extrude anything, and a motion-only
      file declares zero grams anyway (its ``slice_info`` usage is zeroed at build).
    * any other end whose peaks were MEASURED (``peaks_reliable``) → ``partial``, scaled by the
      payload's ``last_progress`` / ``last_layer_num`` — a measured zero included (a job stopped
      before its first layer charges nothing, honestly).
    * any other end whose peaks were NOT measured (a client that attached mid-job — a restart or
      reconnect — or the reconcile's synthesis): ``partial`` when a non-zero peak was read after the
      attach (the firmware's percent and layer are ABSOLUTE, so a reading taken after the attach
      measures this job; only its zero means nothing), else ``none`` — there is nothing to scale by.

    Why ``none`` rather than a guess whenever nothing measured the job (doctrine rule 8): the ledger
    heals in ONE direction. For a tagged roll — and this fleet is RFID'd — the increase-only AMS
    remain% sync restores an under-charge from the wire; an over-charge never heals (the decrease
    repair needs a 50-point contradiction). A charge nobody measured is exactly the phantom this
    basis exists to end (production 2026-09-16 → 24: ~7.2 kg over 32 charges scaled by ANOTHER
    job's live progress).
    """
    if verdict == STOP_SOURCE_RECONCILE_UNKNOWN:
        return "none"
    if evidence.first_unfed_layer is not None:
        return "partial"
    if raw_status == "completed":
        return "full"
    if evidence.peaks_reliable:
        return "partial"
    read_a_peak = (evidence.last_progress or 0) > 0 or (evidence.last_layer_num or 0) > 0
    return "partial" if read_a_peak else "none"


def farm_retries(plate_check: PlateCheckFacts | None, *, evidence: DepositEvidence) -> bool:
    """Does a ``plate_refused`` terminal RETRY the print rather than hand the plate to a human?

    The plate-check ladder (operator rulings 2026-09-29 and 2026-10-05): the farm presses the
    printer's own "Problem solved, resume"; when that re-check fails the ladder decides ONCE —
    the printer's first farm stop in :data:`PLATE_RECHECK_WINDOW_S`, on a farm unit, with
    nothing deposited, is a retry, sent as :data:`PLATE_CHECK_RETRY_STOP`; anything else leaves
    the print paused for a human's decision, and a window that runs out ends it with
    :data:`PLATE_CHECK_DEADLINE_STOP`. This only reads the recorded decision back. A refused
    plate is retried only when every one of these holds:

    * the job had a plate-check episode whose LAST farm stop is the retry rung's (``last_stop``
      named :data:`PLATE_CHECK_RETRY_STOP`). The decision hold's own stop, an operator's Stop
      (no farm stop at all) and a job the printer ended by itself ESCALATE: a human who stepped
      in owns the plate they stepped in on, and a window nobody answered is the ruling's
      escalation;
    * that stop ENDED the job (:attr:`PlateCheckFacts.farm_stopped`: taken, or not yet
      answered). A stop answered as not taken handed the print to a human;
    * the episode's row bound a FARM unit (``item_id``): a foreign print has nothing to requeue;
    * nothing was deposited (``not evidence.deposited``): a stop that left material on the
      plate is a plate a human must clear, whatever the decision said.

    Anything else — including a terminal whose facts could not be read (``plate_check_facts``
    fails closed to facts with no stop) — escalates. ONE caller: :func:`build_terminal_outcome`.
    """
    return (
        plate_check is not None
        and plate_check.last_stop is not None
        and plate_check.last_stop[0] == PLATE_CHECK_RETRY_STOP
        and plate_check.farm_stopped
        and plate_check.item_id is not None
        and not evidence.deposited
    )


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
    plate_check: PlateCheckFacts | None = None,
    resolved_item_id: int | None = None,
) -> TerminalOutcome:
    """Build THE outcome of one terminal from facts the caller captured before any sink acted.

    ``open_incidents`` must be the SAME projection ``verdict`` was classified over (read
    once, ahead of every closer). ``job_id`` is the terminal's echoed ``subtask_id``:
    the printer's recorded words for a hold count as this terminal's evidence only when
    the hold paused THIS job (``job_identity.is_held_job``) — a hold from another job
    explains nothing about this one.

    The evidence the category and the message are built from is the holds' recorded
    words first (they are WHY the job was held, and a stop wipes the printer's live list,
    so a refused plate's codes survive only there) and then the terminal payload's own
    HMS list, de-duplicated by short code.

    ``plate_check`` (the job's plate-check episode, read before any closer) decides ONE thing:
    whether a ``plate_refused`` verdict retries or escalates (:func:`farm_retries`, the decision
    the ladder recorded as its stop's name). Retry leaves ``plate_refusal`` None — no gate, no
    page — while the verdict and ``recorded_status`` stay as they are, so the unit still records
    ``cancelled`` and is requeued next in line. An operator's own Stop mid-episode leaves no farm
    stop step, so it always escalates. The default escalates, so a caller with no plate-check
    facts (the downtime reconcile's unobserved job phase) can never retry.

    ``resolved_item_id`` is the unit THIS terminal resolved — the one ``farm_policy`` requeues. A
    retry the episode's row decided for a unit the terminal did not resolve (none, or another)
    has no requeue target this builder may invent: it is logged with both ids and ESCALATES, so
    a human sees the plate rather than the retry going nowhere.
    """
    this_job = [incident for incident in open_incidents if is_held_job(job_id, str(incident.get("job_id") or ""))]
    recorded = [message for incident in this_job for message in _recorded_messages(incident)]
    printer_evidence = unique_messages([*recorded, *messages_from_payload(hms_errors)])

    recorded_status = _recorded_status(raw_status, verdict, evidence, first_article=first_article, is_eject=is_eject)
    ended_without_part = recorded_status != "completed"
    # A FINISH recorded failed for printing on air: the measured layer IS the failure's cause, ahead
    # of whatever the printer's codes say (they describe the fault before the resume, not the air).
    printed_without_filament = _printed_without_filament(raw_status, evidence, is_eject=is_eject)

    category: str | None = None
    if printed_without_filament:
        category = PRINTED_WITHOUT_FILAMENT_CATEGORY
    elif ended_without_part:
        category = next(
            (_HMS_FAILURE_REASONS[m.short_code] for m in printer_evidence if m.short_code in _HMS_FAILURE_REASONS),
            None,
        )
        if category is None and verdict in OPERATOR_STOP_VERDICTS:
            category = USER_CANCELLED_CATEGORY

    message_parts = [_printed_without_filament_sentence(evidence)] if printed_without_filament else []
    if printer_words := summary_of(printer_evidence):
        message_parts.append(printer_words)

    retries = verdict == STOP_VERDICT_PLATE_REFUSED and farm_retries(plate_check, evidence=evidence)
    if retries and plate_check is not None and plate_check.item_id != resolved_item_id:
        logger.warning(
            "terminal_outcome: job %s — its plate-check episode decided a retry of unit %s, but this terminal "
            "resolved %s; nothing is requeued for the retry, the plate is escalated to a human",
            job_id or "-",
            plate_check.item_id,
            f"unit {resolved_item_id}" if resolved_item_id is not None else "no unit",
        )
        retries = False

    refusal: PlateRefusal | None = None
    if verdict == STOP_VERDICT_PLATE_REFUSED and not retries:
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
        printer_message="; ".join(message_parts) if ended_without_part and message_parts else None,
        plate_refusal=refusal,
        charge=_charge_basis(raw_status, verdict, evidence),
    )
