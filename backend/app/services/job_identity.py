"""THE job-identity comparison: are two printer subtask ids the same print job?

A printer names the job it runs by its ``subtask_id``; for a farm dispatch that is the id the
dispatcher minted (``PrintQueueItem.dispatch_subtask_id``), echoed back on every push and on the
terminal. Every question of the form "is this the job that …" is this one comparison, and it has
three answers, not two — an absent id is not evidence of a DIFFERENT job, so each caller decides
what ``unknown`` is worth to it instead of inheriting a ``""`` string equality that silently
calls two id-less jobs the same one. The one caller-independent reading of ``unknown`` —
"is this the job a hold recorded" — is :func:`is_held_job`, so a hold's question is asked one way.

**Dependency-free by construction** (stdlib only), so a leaf may take it at module level:
``dispatch_claim`` (a leaf that imports no farm service) and ``incident_resolution`` (which must
not import anything that closes) both do. ``print_binding`` — the owner of print ↔ archive
binding, which imports the database — re-exports it for its own callers. Pinned by
``test_code_quality.TestPrintRecordResolution``: a ``subtask_id ==`` comparison outside the owners
is a second spelling.
"""

from __future__ import annotations

from typing import Literal

JobIdentity = Literal["same", "other", "unknown"]

# The printer's word for "this print names no job": Bambu reports "0" for a LAN / non-cloud print and
# an empty id on a screen restart. Neither identifies anything, on either side of a comparison. The
# dispatcher never mints either (``bambu_mqtt`` submission ids are >= 1).
NO_JOB_IDS: frozenset[str] = frozenset({"", "0"})


def job_id(raw: object) -> str | None:
    """A subtask id normalised to the stripped string, or None when it names no job."""
    if raw is None:
        return None
    text = str(raw).strip()
    return None if text in NO_JOB_IDS else text


def same_job(live: str | None, record: str | None) -> JobIdentity:
    """Are these two subtask ids the same print job?

    ``unknown`` when either side names no job (None, ``""`` or ``"0"``) — an absent id is not
    evidence of a DIFFERENT job, and callers decide what an unknown is worth to them (the terminal
    lookup accepts it, the adopt accepts it only for the sole printing unit, the dead-claim judge
    reads it as "not started"). ``same`` / ``other`` only when both sides name a job.
    """
    live_id, record_id = job_id(live), job_id(record)
    if live_id is None or record_id is None:
        return "unknown"
    return "same" if live_id == record_id else "other"


def is_held_job(live: str | None, recorded: str | None) -> bool:
    """Is ``live`` the job a HOLD recorded — the one reading every held-job question shares?

    :func:`same_job`, with ``unknown`` read the way a hold reads a missing id: a hold that
    recorded no job (the printer named none when it paused — a LAN or screen print) is held
    by an echo that names none either, because both sides describe the same id-less job;
    an id on only ONE side is a different job, never a match. ``""`` and ``"0"`` are the
    same "names no job" on either side.

    The rule's one owner (2026-09-29). It was spelled three ways — the rule table's own
    helper, and a hand ``str(...).strip() ==`` in the terminal classifier and in the
    outcome builder — and the hand spellings called a ``"0"`` echo a different job from a
    ``""`` record, so an id-less trip whose terminal echoed ``"0"`` over a hold recorded as
    ``""`` would read as another job's terminal.
    """
    verdict = same_job(live, recorded)
    if verdict == "unknown":
        return job_id(live) is None and job_id(recorded) is None
    return verdict == "same"
