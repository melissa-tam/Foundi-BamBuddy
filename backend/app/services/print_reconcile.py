"""The downtime reconcile: what became of a print whose terminal the farm never saw.

Owned by ``print_binding`` (the owner of print ↔ archive binding) and kept beside it because the
binding module is already the size of its own job. It answers ONE question per printer, once per
MQTT session: the printer's live archive — at most one, by ``ux_print_archives_live_printer`` —
names a job; is that job still running, and if not, what does the farm owe its record?

**Why it was rebuilt (RC3, production 2026-09-16 → 09-24).** The old reconcile read "this
archive's job ended at an unobserved time" as a PRINTER terminal and replayed it through
``main.on_print_complete`` even while the printer demonstrably ran ANOTHER job. Every
printer-scoped consumer then acted on the running job: a plate gate raised mid-print (the 90-minute
"Plate Not Empty – print paused" page of the 09-20 00:19Z burst), a foreign-job page, filament
charged from the LIVE job's progress (~7.2 kg over 32 phantom charges), the running job's usage
session popped, an RFID sweep, swap and de-bounce resets, and the wire-hold closer. It also judged
on the CACHED previous-session state ``_on_connect`` re-broadcasts, so a restart could replay a
healthy print.

Now: a pure :func:`judge` over frozen :class:`ArchiveEvidence`, table-tested in the
``dispatch_claim.judge`` shape; the gathering and the applying live in :func:`reconcile_printer`;
and a terminal is TWO phases — the JOB phase (``job_terminal``: the record, the unit, the
print-log row, the uploaded file) and the PRINTER phase (the rest of ``main.on_print_complete``,
keyed to the job the printer runs NOW). Each verdict runs exactly the phases it names.
``main`` keeps only the connected-edge hook, fired on the FIRST FRESH REPORT of each session.

The same job phase closes an archive superseded at print start
(``print_binding.supersede_other_live``), so a record's end reads the same whichever lane found it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, Protocol

# Through the module: the session is read at CALL time (see ``job_terminal``).
from backend.app.core import database as _database
from backend.app.core.database import hold_write_lock
from backend.app.services import farm_correlation, job_terminal, print_binding
from backend.app.services.farm_correlation import PAYLOAD_KEY_OUTCOME_UNKNOWN
from backend.app.services.job_identity import same_job
from backend.app.services.plate_occupancy import ACTIVE_PRINT_STATES, DepositEvidence, plate_occupancy
from backend.app.services.queue_transitions import UNIT_TERMINAL_STATUSES

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services.bambu_mqtt import PrinterState
    from backend.app.services.plate_occupancy import TerminalDisposition

logger = logging.getLogger(__name__)

#: What one live archive's evidence adds up to. Closed on purpose: a sixth reading of "what became
#: of this print" is added here, where the table can be asked about it.
ReconcileVerdict = Literal[
    "running",  # leave it: the job may still be running, or the state cannot say
    "observed",  # its run already recorded how it ended: close the record to match (job phase only)
    "superseded",  # the printer runs ANOTHER job: record the end unknown (job phase only)
    "ended",  # the printer ended THIS job: the full terminal, both phases, bound to this archive
    "ended_unattributed",  # the printer ended a job it cannot say is this one: printer phase for the
    # live job (a source-less plate hold) + this archive's superseded job phase
]

# The printer's word for "no job is running": a job ENDED. Upper-cased gcode_state.
_ENDED_STATES: frozenset[str] = frozenset({"IDLE", "FINISH", "FAILED"})
# The terminal states whose word is evidence of HOW a job ended.
_OUTCOME_STATES: dict[str, str] = {"FINISH": "completed", "FAILED": "failed"}


@dataclass(frozen=True)
class ArchiveEvidence:
    """Everything :func:`judge` is allowed to know about one live archive.

    Gathered by :func:`reconcile_printer` from the printer's live state and the database; frozen
    so a verdict can be re-derived from the same facts in a test and logged beside what it did.
    """

    #: The printer's gcode_state, upper-cased; ``""`` when it has none.
    live_state: str
    #: The live state describes THIS MQTT session: ``connected`` and its first report applied
    #: (``report_epoch == connection_epoch``). ``_on_connect`` re-broadcasts the PREVIOUS session's
    #: cached state before its pushall answers — a state that is not fresh is no evidence.
    live_fresh: bool
    #: The job id the printer echoes (raw; :func:`same_job` normalises).
    live_job: str | None
    #: The archive's own job id (``PrintArchive.subtask_id``).
    archive_job: str | None
    #: The recorded end of the archive's RUN UNIT — the unit on that printer whose
    #: ``dispatch_subtask_id`` is the archive's job id — or None when there is none or it has not
    #: ended (still ``printing``).
    run_unit_status: str | None


def judge(evidence: ArchiveEvidence) -> ReconcileVerdict:
    """Read one live archive's evidence into a verdict. Pure — no DB, no wire, no clock.

    The rows, in precedence order (first match wins), each with its reason for being ahead of the
    next:

    1. **running** — the live state is unknown or not fresh, or it is ACTIVE (``PREPARE``,
       ``SLICING``, ``RUNNING``, ``PAUSE`` — the plate authority's one spelling) with the SAME
       job or one it cannot compare (``unknown``). A decision against a cached or unrecognised
       state is a guess; and an active printer that may be running this very job must be left to
       its own terminal. Ahead of everything: its cost when wrong is a record left open until the
       next reconnect, the opposite row's cost is RC3.
    2. **observed** — the run unit already ENDED. How the print ended is recorded; the farm only
       lost the archive (the RC1 leak). The record takes the unit's outcome — never a printer
       phase, which would act on whatever the printer does now. Ahead of 3-5 because the unit's
       recorded end outranks anything the live state could suggest about it.
    3. **superseded** — the printer is ACTIVE on ANOTHER job. This job is over and nobody saw how:
       the job phase with an unknown outcome, and NO printer phase — the plate, the eject, the
       usage session and the holds are the running job's.
    4. **ended** — the printer ENDED (IDLE / FINISH / FAILED) and names THIS job: the full
       terminal, bound to this archive by id. FINISH / FAILED carry the true outcome; IDLE says
       only that it ended.
    5. **ended_unattributed** — the printer ENDED, but names another job or none. Something left
       the plate in an unknown state — so the plate is held for a human, source-less (it is not
       this unit's part to sweep) — and this job's end is unknown: the superseded job phase.
    """
    identity = same_job(evidence.live_job, evidence.archive_job)
    active = evidence.live_state in ACTIVE_PRINT_STATES
    if not evidence.live_fresh or not (active or evidence.live_state in _ENDED_STATES):
        return "running"
    if active and identity != "other":
        return "running"
    if evidence.run_unit_status is not None:
        return "observed"
    if active:
        return "superseded"
    if identity == "same":
        return "ended"
    return "ended_unattributed"


def is_fresh(state: PrinterState) -> bool:
    """Does ``state`` describe the printer's CURRENT MQTT session? ONE spelling, for the
    connected-edge hook in ``main`` and for the evidence."""
    return bool(state.connected) and state.report_epoch is not None and state.report_epoch == state.connection_epoch


def evidence_of(state: PrinterState, archive: PrintArchive, run_unit: PrintQueueItem | None) -> ArchiveEvidence:
    """Gather :class:`ArchiveEvidence` from the live state and the two rows."""
    return ArchiveEvidence(
        live_state=(state.state or "").upper(),
        live_fresh=is_fresh(state),
        live_job=state.subtask_id,
        archive_job=archive.subtask_id,
        run_unit_status=run_unit.status if run_unit is not None and run_unit.status in UNIT_TERMINAL_STATUSES else None,
    )


class TerminalHandler(Protocol):
    """The full terminal (``main.on_print_complete``), injected: a service never imports ``main``."""

    async def __call__(self, printer_id: int, data: dict, *, archive_id: int | None = None) -> None: ...


def ended_payload(state: PrinterState, archive: PrintArchive) -> dict:
    """The terminal an ``ended`` verdict synthesises — the payload the MQTT client would have sent.

    FINISH / FAILED of THIS job is real evidence of the outcome, so the TRUE status rides with
    the live progress and layer (Phase 3.4) and the one normal terminal path runs on it (the gate,
    the identity cooldown watch, the farm policy). IDLE says only that it ended: ``aborted`` plus
    ``outcome_unknown`` (``farm_correlation.PAYLOAD_KEY_OUTCOME_UNKNOWN``), which the one classifier
    turns into the ``reconcile_unknown`` verdict — the run HOLDS for a human instead of finishing
    one plate short. Both carry ``peaks_reliable: False``: nobody observed this print's peaks, and
    ``DepositEvidence`` fails closed on that (the 2026-08-29 cascade).
    """
    outcome = _OUTCOME_STATES.get((state.state or "").upper())
    if outcome is not None:
        return {
            "status": outcome,
            "filename": archive.filename,
            "subtask_name": archive.print_name or "",
            "subtask_id": state.subtask_id,
            "last_progress": state.progress,
            "last_layer_num": state.layer_num,
            "peaks_reliable": False,
            "raw_data": state.raw_data or {},
            "_reconciled": True,
        }
    return {
        "status": "aborted",
        "filename": archive.filename,
        "subtask_name": archive.print_name or "",
        "subtask_id": archive.subtask_id or "",
        "peaks_reliable": False,
        "raw_data": state.raw_data or {},
        "_reconciled": True,
        PAYLOAD_KEY_OUTCOME_UNKNOWN: True,
    }


async def _unattributed_hold(db: AsyncSession, printer_id: int, state: PrinterState) -> TerminalDisposition:
    """The printer phase of ``ended_unattributed``: the disposition of a deposit nobody can attribute.

    The printer ended a job it cannot say is this archive's, so what sits on the plate is unknown
    — possibly another job's part. The plate fails closed (``DepositEvidence.unknown``) under a
    SOURCE-LESS escalation-only hold, the refused-plate branch's shape
    (``farm_correlation.terminal_disposition``): no unit, no job id, so no automatic sweep can pair
    it with this archive's unit and sweep a part that is not its own. The raise guard is the real
    terminal's for a deposit no unit owns — the ``require_plate_clear`` toggle, or farm work
    targeting the printer.
    """
    from backend.app.api.routes.settings import get_setting

    raw_rpc = await get_setting(db, "require_plate_clear")
    require_plate_clear = raw_rpc is None or raw_rpc.strip().lower() == "true"
    raise_gate = require_plate_clear or await farm_correlation.farm_work_targets_printer(db, printer_id)
    return farm_correlation.terminal_disposition(
        verdict="none",
        item_id=None,
        eject_profile_id=None,
        first_article=False,
        batch_id=None,
        source_subtask_id=None,
        evidence=DepositEvidence.unknown(_OUTCOME_STATES.get((state.state or "").upper(), "aborted")),
        raise_gate=raise_gate,
    )


async def reconcile_printer(printer_id: int, state: PrinterState | None, *, terminal: TerminalHandler) -> int:
    """Reconcile the printer's live archive against its live state; the number of archives acted on.

    Called once per MQTT session by ``main``'s connected-edge hook, on the session's first fresh
    report. Gathers under the SQLite write lock (the job phase reads, then writes, in one
    transaction), judges, and applies exactly what the verdict names:

    =====================  ======================================================================
    verdict                applies
    =====================  ======================================================================
    running                nothing
    observed               job phase: the record closed with the run unit's outcome + its missing
                           print-log row (``job_terminal.close_observed``)
    superseded             job phase, outcome unknown (``job_terminal.close_superseded``): the
                           unit ended ``cancelled`` / ``reconcile_unknown`` and held, a
                           ``cancelled`` print-log row, the uploaded file removed (never the
                           running job's) — no charge, NO printer phase
    ended                  ``terminal(printer_id, payload, archive_id=…)`` — the full terminal
                           bound to THIS archive by id, never re-resolved by name
    ended_unattributed     printer phase for the LIVE job: a source-less escalation-only plate
                           hold; plus this archive's superseded job phase
    =====================  ======================================================================

    Guarded: a failure is logged and the archive stays live for the next session to retry — the
    connected edge is a hot path of the status broadcast.
    """
    if state is None or not state.connected:
        return 0
    try:
        closed: job_terminal.ClosedJob | None = None
        hold: TerminalDisposition | None = None
        async with _database.async_session() as db:
            await hold_write_lock(db)
            archive = await print_binding.live_print_archive(db, printer_id)
            if archive is None:
                return 0
            run_unit = await print_binding.unit_of_print_archive(db, archive.id)
            evidence = evidence_of(state, archive, run_unit)
            verdict = judge(evidence)
            logger.info(
                "[RECONCILE] printer %s: live archive %s (job %r) — %s (live %s job %r fresh=%s, run unit %s %s)",
                printer_id,
                archive.id,
                archive.subtask_id,
                verdict,
                evidence.live_state or "-",
                evidence.live_job,
                evidence.live_fresh,
                run_unit.id if run_unit is not None else None,
                run_unit.status if run_unit is not None else None,
            )
            if verdict == "running":
                return 0
            if verdict == "ended":
                payload = ended_payload(state, archive)
                archive_id = archive.id
            else:
                now = datetime.now(timezone.utc)
                if verdict == "observed":
                    assert run_unit is not None  # the judge's observed row needs an ended run unit
                    closed = await job_terminal.close_observed(db, archive, run_unit, printer_id=printer_id)
                else:
                    other_job = same_job(evidence.live_job, evidence.archive_job) == "other"
                    keep_paths = (
                        await job_terminal.live_upload_paths(db, printer_id, state.subtask_id, state.subtask_name)
                        if verdict == "superseded"
                        else frozenset()
                    )
                    closed = await job_terminal.close_superseded(
                        db,
                        archive,
                        run_unit,
                        printer_id=printer_id,
                        reason=print_binding.SUPERSEDED_REASON if other_job else None,
                        keep_paths=keep_paths,
                        now=now,
                    )
                    if verdict == "ended_unattributed":
                        hold = await _unattributed_hold(db, printer_id, state)
                await db.commit()

        if verdict == "ended":
            await terminal(printer_id, payload, archive_id=archive_id)
            return 1
        if hold is not None:
            plate_occupancy.note_terminal(printer_id, hold)
            logger.warning(
                "[RECONCILE] printer %s: plate held for a human — the printer ended a job it cannot name as "
                "archive %s's (live %s job %r)",
                printer_id,
                archive.id,
                evidence.live_state,
                evidence.live_job,
            )
        if closed is None:
            return 0
        await job_terminal.settle(closed)
        return 1
    except Exception as e:  # noqa: BLE001 — a reconcile failure must not break the status flow
        logger.warning("[RECONCILE] printer %s: reconcile failed (the next session retries): %s", printer_id, e)
        return 0
