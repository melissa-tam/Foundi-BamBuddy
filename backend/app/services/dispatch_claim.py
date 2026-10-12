"""What a dispatch claim's evidence means — the START's answer first, production's two bounds behind it
(owner 9, 2026-10-11; R17/R18 of the 2026-10-12 restoration).

A unit is marked ``printing`` BEFORE the print command goes out (deliberately: a crash in between
must leave a row wrongly marked printing rather than one that silently reprints hours later). What
then decides the claim is the dispatch's ``print_start`` action on the record
(``services/printer_action`` + ``command_outcome``'s start family), answered by an EVENT:

* **reached** — the printer reported the dispatched job's own id in an ACTIVE job word, or that job's
  terminal (a FAILED with its id, or its terminal event): the job is the printer's, and the claim
  stands (its terminal does the rest). The same id at IDLE / FINISH is only a LANDING (the non-final
  ``Reading.LANDED``): the file landed, the job has not begun;
* **refused** — the printer's fail ACK, or a frame that never left the farm: the printer never took
  the job, the unit goes back to the queue next in line;
* **void** — a later session began with the start unacknowledged and its job never reported, or the
  printer rebooted: the printer may never have received it, the unit goes back;
* **taken over** — another job took the printer: the unit never printed and goes back (loudly), while
  the other job is the FOREIGN path's.

An outstanding start waits for its event, and production's self-heal bounds are the fallback
(operator ruling 2026-10-12: "if it was self heal, it should stay self heal"), counted by the record in
the printer's ONLINE seconds — the bounds themselves are ``services/dispatch_start``'s:

* no answer and no landing by ``_START_SELF_HEAL`` (90 s from the send; production
  ``_watchdog_print_start`` Phase A) → the unit goes back, :data:`UNANSWERED_CAUSE`;
* landed but never active by ``_START_ACTIVE_SELF_HEAL`` (180 s from the landing; Phase B, #1678) → the
  unit goes back, :data:`NOT_ACTIVE_CAUSE`.

An answer that arrives AFTER its bound returned the unit is **late** (:func:`start_late`): it touches no
claim and no lease — the lease may already be a new dispatch's — and a print it reached runs as the
FOREIGN path's (production's #1150 risk).

A claim the RECORD cannot answer — no ``print_start`` action exists for it (a claim committed by a
build before the record, or a crash between the claim and the send) — is judged ONCE, at the printer's
first fresh report of a session, by :func:`judge` over the printer's own report.

**Leaf by construction.** It imports no farm service and holds no DB handle or process state: the
applying (the release, the lease, the log) is ``services/dispatch_start``'s. The two imports are
owners of a definition a second spelling would fork: ``ACTIVE_PRINT_STATES``, taken from the
plate-occupancy authority that OWNS "what counts as an active job" (a second spelling of the set is
how two lanes come to disagree about PAUSE — here the difference between leaving a native-vision hold
alone and double-dispatching onto an occupied plate); and ``job_identity.same_job``, the ONE
comparison of two subtask ids. ``command_outcome.Answer`` is the record's own answer vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from backend.app.services.command_outcome import Answer
from backend.app.services.job_identity import same_job
from backend.app.services.plate_occupancy import ACTIVE_PRINT_STATES

#: What a claim's start answer means. Closed on purpose: a new reading of "did this dispatch start"
#: is added here, where the table can be asked about it. ``late`` — an answer that came after the start's
#: self-heal bound already returned the unit (:func:`start_late`): nothing is touched.
StartVerdict = Literal["started", "waiting", "refused", "void", "taken_over", "late"]

#: The return cause each un-started verdict hands ``requeue.return_to_queue`` (its ``ReturnCause``).
RETURN_CAUSES: dict[StartVerdict, str] = {
    "refused": "start_refused",
    "void": "start_void",
    "taken_over": "start_taken_over",
}

#: The cause of a claim the record cannot answer and the printer's report shows never started.
UNRECORDED_CAUSE = "start_unrecorded"

#: The causes of a start the printer stayed silent on past its self-heal bound (production
#: ``_watchdog_print_start``, 337f4954): no answer and no landing by ``_START_SELF_HEAL`` (Phase A, #967) —
#: or the job landed and never became active by ``_START_ACTIVE_SELF_HEAL`` (Phase B, #1678).
UNANSWERED_CAUSE = "start_unanswered"
NOT_ACTIVE_CAUSE = "start_not_active"

_VERDICT_OF: dict[Answer, StartVerdict] = {
    Answer.REACHED: "started",
    Answer.REFUSED: "refused",
    Answer.VOID: "void",
    Answer.TAKEN_OVER: "taken_over",
}


def verdict_of(answer: Answer | None, *, late: bool) -> StartVerdict:
    """The claim's verdict from its ``print_start`` action's answer — ``None`` (still outstanding) WAITS
    for its event or its self-heal bound; an answer :func:`start_late` calls late is ``late``, whatever it
    is. Total over :class:`~backend.app.services.command_outcome.Answer`: a new answer raises ``KeyError``
    here instead of falling through to a silent requeue."""
    if answer is None:
        return "waiting"
    if late:
        return "late"
    return _VERDICT_OF[answer]


def start_late(*, bound_passed: bool, unit_status: str | None, current_start: int | None, action_id: int) -> bool:
    """Is an ANSWERED start late — its self-heal bound passed (``printer_action.bound_passed_at``) and its unit
    has moved on without it: no longer ``printing``, or a newer dispatch's start names it
    (``printer_action.last_start_of``)? Then the bound's fallback already returned the unit, and the lease on
    the printer may be a NEW dispatch's: a late answer touches no claim and no lease. A start whose bound
    passed but whose unit is still ``printing`` on it is not late — the fallback has not acted (or could
    not), and the answer is applied as any other: whichever comes first wins."""
    return bound_passed and (unit_status != "printing" or current_start != action_id)


def fallback_cause(*, landed: bool) -> str:
    """The return cause of a start whose self-heal bound passed with no answer: the printer reported the
    job's id (landed, never active — Phase B) or nothing at all (Phase A)."""
    return NOT_ACTIVE_CAUSE if landed else UNANSWERED_CAUSE


ClaimVerdict = Literal["started", "dead"]


@dataclass(frozen=True)
class ClaimEvidence:
    """What :func:`judge` may know about one ``printing`` claim the RECORD cannot answer, read at the
    printer's first fresh report of a session (``services/dispatch_start.reevaluate_session``).
    Frozen so a verdict can be re-derived from the same facts in a test and logged verbatim beside
    the release it produced."""

    #: The printer's live gcode state, upper-cased; ``""`` when it has none yet.
    live_state: str
    #: Some ``PrintArchive`` on this printer reads ``printing``.
    archive_printing: bool
    #: The submission id the dispatcher minted for this unit; ``""`` when it has none.
    dispatch_subtask: str
    #: The job id the printer is echoing right now; ``""`` when it names none.
    live_subtask: str


def judge(evidence: ClaimEvidence) -> ClaimVerdict:
    """Read an UNRECORDED claim's evidence into a verdict. Pure — no DB, no wire, no clock.

    ``started``, three ways, all double-dispatch guards (the cost of a wrong release is a print onto
    an occupied plate):

    * the live state is ACTIVE — **PAUSE included**: a native-vision trip pauses at print start with
      the plate occupied, and releasing that unit would re-dispatch onto it. An EMPTY state reads as
      started too: no state is not evidence;
    * a ``PrintArchive`` on this printer reads ``printing`` — hard disjointness with the downtime
      reconcile, so the two can never both act on one printer;
    * the printer is echoing OUR dispatch id, in any job word. Corroboration, never a precondition: an
      id missing on either side is ``unknown``.

    Anything else is ``dead``: the printer's first fresh report of a session shows no job of ours and
    no job at all, and nothing on the record can ever answer the claim — it goes back to the queue.
    """
    if not evidence.live_state or evidence.live_state in ACTIVE_PRINT_STATES:
        return "started"
    if evidence.archive_printing:
        return "started"
    if same_job(evidence.live_subtask, evidence.dispatch_subtask) == "same":
        return "started"
    return "dead"
