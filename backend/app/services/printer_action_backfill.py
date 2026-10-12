"""The backfill of the action record for every step that names no action (2026-10-11; every boot since 2026-10-12).

``printer_action`` is the durable record of every command a printer is sent and of its answer; a
driver's step (``printer_incident_step``) references the action its send was (``action_id``). Steps
written by the builds before it carry no action. This backfill gives each one its action, so the
record holds the whole history and the open-row projection (``printer_incidents._ledger_projections``)
reads the pending command of a row opened before the cutover from the record like any other.

**Every boot, never once.** This build writes every step with its action at the INSERT
(``printer_incidents.EvidenceLog.note``: each driver step is drafted with the action it sent), so an
unlinked step is always a previous build's — before the cutover, or during a rollback window after it.
A once-marker would leave a rollback window's steps unlinked when this build returns, and their pending
command invisible (AMS shape 46); so ``core.database.run_migrations`` runs it on every boot. A boot with
nothing to link costs the one indexed read of the ``action_id IS NULL`` set
(``ix_printer_incident_step_action``) and logs nothing.

**Planned from the live database, re-verified per row.** For each unlinked step it reads the step and
its incident, writes ONE action row (the step's own facts: printer, command, target, the job the
incident held, the send time) and links the step — the link's ``UPDATE`` restates ``action_id IS NULL``,
so a step another writer linked meanwhile is skipped, never double-linked. Idempotent: a second run
finds nothing unlinked.

**The answer, by an explicit table.** A step's own reading is the driver's vocabulary; the record's
answer is one of four (``command_outcome.Answer``). :data:`_ANSWER_OF` maps every (kind, outcome) pair
the drivers write; a pair it does not name is REPORTED (the step is left unlinked and logged), never
guessed. ``_OPEN_OUTSTANDING`` marks the readings that mean "the printer has not answered yet" — on an
OPEN row the action stays outstanding (the record answers it on the next event: a held load that
lands, a stop whose terminal arrives); on a CLOSED row nothing can answer it any more, so it is VOID.
A step never answered (``outcome`` NULL) follows the same rule. The readings the driver produced at a
CLOCK (``acted`` / ``no_movement`` / ``session_changed`` …) say nothing the record can stand on: VOID,
with the reading kept in ``reason``.

**Standalone by construction:** the standard library and SQLAlchemy only, on the migration's own
``AsyncConnection``; ``core.database.run_migrations`` owns the outer savepoint and the logging.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_REACHED: Final = "reached"
_REFUSED: Final = "refused"
_VOID: Final = "void"
_OPEN_OUTSTANDING: Final = "open_outstanding"
# A driver's "the frame never went out" reading, and the record's refusal token for it
# (``printer_action.Refusal``).
_NOT_SENT: Final = "not_sent"

AnswerOrOpen = Literal["reached", "refused", "void", "open_outstanding"]

# (step kind, step outcome) → the record's answer. EXPLICIT: an unnamed pair is reported, never guessed.
_ANSWER_OF: Final[dict[tuple[str, str], AnswerOrOpen]] = {
    # AMS motion commands (``command_outcome.MotionReading``)
    ("command", "complete"): _REACHED,
    ("command", "held"): _OPEN_OUTSTANDING,
    ("command", "acted"): _VOID,
    ("command", "no_movement"): _VOID,
    ("command", "session_changed"): _VOID,
    ("command", "undecidable"): _VOID,
    # release levers (``spool_recovery.LeverReading``)
    ("lever", "self_healed"): _REACHED,
    ("lever", "swapped"): _REACHED,
    ("lever", "stopped"): _REACHED,
    ("lever", "not_sent"): _REFUSED,
    ("lever", "wedged"): _VOID,
    ("lever", "released"): _VOID,
    ("lever", "ended"): _VOID,
    ("lever", "no_pause"): _VOID,
    ("lever", "abort"): _VOID,
    ("lever", "handover"): _VOID,
    # the print laid down a layer on air inside the lever's window and the farm's air stop took over
    # (2026-10-12): the lever did not release anything
    ("lever", "air_stopped"): _VOID,
    # plate-check dialog presses (``pause_recovery.PlateCheckDialogAnswer``)
    ("dialog", "success"): _REACHED,
    ("dialog", "fail"): _REFUSED,
    ("dialog", "not_sent"): _REFUSED,
    ("dialog", "void"): _VOID,
    ("dialog", "no_ack"): _VOID,
    # the re-check's self-heal bound passed with the press unanswered (2026-10-12): the record keeps it open
    ("dialog", "bound_passed"): _OPEN_OUTSTANDING,
    # plate-check stops (``pause_recovery.PlateCheckStopAnswer``)
    ("stop", "taken"): _REACHED,
    ("stop", "not_sent"): _REFUSED,
    ("stop", "not_taken"): _OPEN_OUTSTANDING,
    # the stop's self-heal bound passed with no event (2026-10-12): the record keeps it open
    ("stop", "unconfirmed"): _OPEN_OUTSTANDING,
}

# A lever's name → the record's command (``command_outcome.COMMANDS``).
_LEVER_COMMAND: Final[dict[str, str]] = {
    "resume": "resume",
    "resume_then_pause": "resume",
    "ams_control_resume": "ams_control_resume",
    "retry_after_clear": "ams_control_resume",
    "clean_print_error": "clean_print_error",
    "ams_control_abort": "ams_control_abort",
    "ams_control_reset": "ams_control_reset",
    "ams_control_pause": "ams_control_pause",
    "print_stop": "stop",
}

# The plate-check dialog buttons only a PERSON presses through the farm (the ladder presses only its
# own "Problem solved, resume"): ``printer_incidents.PLATE_CHECK_HUMAN_ACTIONS`` minus that button.
_HUMAN_ONLY_BUTTONS: Final = frozenset({"IGNORE_RESUME"})

# The feeder kinds a command step recorded that name a real AMS feeder at the extruder.
_LOADED_FEEDERS: Final = frozenset({"jammed", "other"})
_EMPTY_FEEDERS: Final = frozenset({"empty", "external"})

_CLOSED_STATUSES: Final = ("resolved", "aborted")


@dataclass(slots=True)
class ActionBackfillTally:
    """What one run of the backfill did, and the lines it owes the log (written once applied)."""

    linked: int = 0
    outstanding: int = 0
    skipped: int = 0
    lines: list[tuple[int, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.linked} step(s) linked to a backfilled action ({self.outstanding} left outstanding on an "
            f"open hold), {self.skipped} left unlinked on an outcome with no mapping"
        )


def _command_of(kind: str, name: str) -> str | None:
    if kind == "command":
        return name if name in ("load", "unload") else None
    if kind == "lever":
        return _LEVER_COMMAND.get(name)
    if kind == "dialog":
        return "dialog_press"
    if kind == "stop":
        return "stop"
    return None


def _entry_of(kind: str, feeder: str | None) -> str | None:
    """A command step's recorded feeder as the record's entry posture: the family part is unknown
    (``other``), the loaded / empty part is what the outcome reading needs."""
    if kind != "command" or feeder is None:
        return None
    if feeder in _LOADED_FEEDERS:
        return "other_loaded"
    if feeder in _EMPTY_FEEDERS:
        return "other_empty"
    return None


async def backfill_step_actions(conn: AsyncConnection) -> ActionBackfillTally:
    """Give every unlinked step its action (module docstring). The caller owns the outer savepoint."""
    tally = ActionBackfillTally()
    rows = (
        await conn.execute(
            text(
                "SELECT s.id, s.incident_id, s.kind, s.name, s.target, s.feeder, s.outcome, s.sent_at, s.read_at, "
                "i.printer_id, i.job_id, i.status "
                "FROM printer_incident_step s JOIN printer_incident i ON i.id = s.incident_id "
                "WHERE s.action_id IS NULL ORDER BY s.id"
            )
        )
    ).all()
    for row in rows:
        step_id, incident_id, kind, name, target, feeder, outcome, sent_at, read_at, printer_id, job_id, status = row
        command = _command_of(kind, name)
        if command is None:
            tally.skipped += 1
            tally.lines.append(
                (logging.WARNING, f"step {step_id} ({kind}={name!r}) names no record command -- left unlinked")
            )
            continue
        closed = status in _CLOSED_STATUSES
        if outcome is None:
            mapped: AnswerOrOpen = _OPEN_OUTSTANDING
        else:
            found = _ANSWER_OF.get((kind, outcome))
            if found is None:
                tally.skipped += 1
                tally.lines.append(
                    (
                        logging.WARNING,
                        f"step {step_id} ({kind}={name!r}) outcome {outcome!r} has no mapping -- left unlinked",
                    )
                )
                continue
            mapped = found
        refusal = None
        if mapped == _OPEN_OUTSTANDING:
            answer, reason, answered_at = (
                (None, None, None)
                if not closed
                else (_VOID, f"pre-record step: {outcome or 'never answered'}", read_at or sent_at)
            )
        else:
            answer = mapped
            answered_at = read_at or sent_at
            if outcome == _NOT_SENT:
                # The frame never left the farm: the record's ``refusal`` token, never the printer's
                # words — so the once (``incident_resolution.resume_spent``) is not spent by it.
                refusal, reason = _NOT_SENT, None
            else:
                reason = None if mapped == _REACHED else f"pre-record step: {outcome}"
        acked_at = read_at if (kind, outcome) == ("command", "held") else None
        actor = "operator" if kind == "dialog" and name in _HUMAN_ONLY_BUTTONS else "farm"
        inserted = await conn.execute(
            text(
                "INSERT INTO printer_action (printer_id, actor, command, target, entry, job_id, incident_id, "
                "sent_at, acked_at, answer, reason, refusal, answered_at) VALUES (:printer_id, :actor, :command, "
                ":target, :entry, :job_id, :incident_id, :sent_at, :acked_at, :answer, :reason, :refusal, "
                ":answered_at) RETURNING id"
            ),
            {
                "printer_id": printer_id,
                "actor": actor,
                "command": command,
                "target": target if kind == "command" else None,
                "entry": _entry_of(kind, feeder),
                "job_id": job_id or None,
                "incident_id": incident_id,
                "sent_at": sent_at,
                "acked_at": acked_at,
                "answer": answer,
                "reason": reason,
                "refusal": refusal,
                "answered_at": answered_at,
            },
        )
        action_id = inserted.scalar_one()
        linked = await conn.execute(
            text("UPDATE printer_incident_step SET action_id = :action_id WHERE id = :step_id AND action_id IS NULL"),
            {"action_id": action_id, "step_id": step_id},
        )
        if linked.rowcount != 1:
            # Linked by another writer between the read and here: drop this run's action row.
            await conn.execute(text("DELETE FROM printer_action WHERE id = :id"), {"id": action_id})
            continue
        tally.linked += 1
        if answer is None:
            tally.outstanding += 1
    return tally
