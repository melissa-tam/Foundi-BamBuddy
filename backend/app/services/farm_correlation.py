"""Terminal-status → queue-item correlation for the farm loop (Phase 1, P1-A).

The terminal-status callback used to find "the finished queue item" by
``printer_id`` + ``status == "printing"`` alone. That silently misattributes when
the printer runs a job Bambuddy did not dispatch: a LOCAL print started from the
touchscreen finishes, and its FINISH gets pinned onto whatever farm unit happened
to be "printing" in the DB — marking the wrong unit done, and (worse) arming the
plate-clear auto-release with the previous unit's cooldown threshold so the gate
clears onto the operator's part and the next unit dispatches onto an occupied
plate (scenario S4).

:func:`resolve_terminal_item` decides, per terminal event, WHICH queue item (if
any) the finish belongs to, returning one of five verdicts:

- ``matched``          — the printer echoed a ``subtask_id`` equal to a printing
                         item's ``dispatch_subtask_id`` (the robust path: the id
                         Bambuddy minted for that exact dispatch).
- ``matched_by_name``  — no id match, but the terminal's project/file name matches
                         the dispatched source name of a printing item that has NO
                         stamped ``dispatch_subtask_id`` (a legacy/pre-migration row
                         dispatched before stamping existed — name matching exists
                         solely to rescue those). A stamped item can only ever be
                         claimed by id equality: a present-but-different payload id
                         means "not this item" regardless of name, because an
                         operator re-printing the SAME file locally mints a fresh id
                         with an identical name (S4/S9). The exact comparison: the
                         payload's ``subtask_name`` and ``filename`` are run
                         through ``utils.filename.print_identity_key`` — basename,
                         trailing ``.gcode.3mf``/``.3mf``/``.gcode`` stripped, the
                         splicer's mid-stem ``.gcode`` token removed, spaces folded
                         to underscores, lower-cased — and must intersect the same
                         key over the item's ``archive.print_name`` /
                         ``archive.filename`` / ``library_file.filename``. That key
                         is shared with the foreign auto-eject identity check
                         (``eject.manual._canonical_names``) so the two answers to
                         "is this the same print?" cannot drift. It is MORE
                         permissive than the pre-2026-08-22 private variant, which
                         folded neither spaces nor the mid-stem ``.gcode`` token and
                         so could not match this farm's spliced corpus at all. The
                         widening is bounded by the ``dispatch_subtask_id IS NULL``
                         precondition: an item the farm dispatched with a stamped key
                         is claimable by id equality ONLY, so a more permissive name
                         key can never re-attribute one.
- ``fallback``         — the terminal carried no ``subtask_id`` at all (firmware
                         that resets it on cancel, or an upgrade-day row dispatched
                         before ``dispatch_subtask_id`` existed) AND there is exactly
                         one printing item. The sole printing unit on the printer is
                         the best attribution; logged at WARNING because it was not
                         id-confirmed.
- ``foreign``          — the terminal carried a ``subtask_id`` that matches NO
                         printing item — EITHER a printing candidate exists whose id
                         differs, OR there are ZERO printing candidates at all (the
                         production S4 case: the farm units were cancelled, then the
                         operator re-started the farm's own USB file from the
                         touchscreen — a fresh firmware-minted id, no printing row).
                         The printer ran something Bambuddy did not dispatch. Farm
                         state MUST NOT be mutated: no unit is marked done, no
                         retry/quarantine is attributed, and no auto-clear is armed.
                         The caller still raises the plate-clear gate (the deposit is
                         real) but keys it human-clear-only.
- ``none``             — nothing to attribute AND nothing to gate on identity: zero
                         printing candidates with NO echoed ``subtask_id`` (a bare
                         state blip we must never guess a deposit from), or the
                         pathological multi-candidate no-id no-name-match case. No
                         queue item to touch.

Why ``foreign`` must never mutate farm state: attribution drives retry counting,
quarantine escalation, run completion, and the cooldown auto-clear threshold. A
print Bambuddy never sent carries none of that identity — treating it as a farm
unit corrupts run accounting and can auto-clear the gate onto a foreign part. The
gate still rises (a human must clear the plate), but the run is left exactly as it
was so the missing unit is simply re-dispatched once the plate is clear.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal

from sqlalchemy import or_, select

from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.dispatch_target import target_of
from backend.app.services.plate_occupancy import (
    CooldownEject,
    DepositEvidence,
    EscalationOnly,
    ForeignAutoEject,
    OccupancyPolicy,
    TerminalDisposition,
    plate_occupancy,
)
from backend.app.services.printer_incidents import (
    WAITING_REASON_PLATE_VISION as WAITING_REASON_PLATE_VISION,  # noqa: PLC0414 — explicit re-export
)
from backend.app.utils.filename import print_identity_key

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

Verdict = Literal["matched", "matched_by_name", "fallback", "foreign", "none"]

# The farm's own abort mark, written onto a ``printing`` row's ``stop_source`` BEFORE
# the stop that produces the terminal. ONE origin for the literal: it is written by
# ``pause_recovery.on_plate_vision_trip``, read back by :func:`classify_stop`, honoured
# by the terminal handler and rendered in the run-detail lineage, and four spellings of
# a durable marker is how one of them ends up not matching.
STOP_SOURCE_FARM_VISION_ABORT = "farm_vision_abort"

# Why a terminal happened, as a CLOSED set. The disposition is selected from this
# verdict once, ahead of the ``final_status`` fork, so a new reason for a print to end
# has to be added here rather than sniffed out of a status string downstream.
StopVerdict = Literal["farm_vision_abort", "operator_ui", "operator_screen"]

# ``WAITING_REASON_PLATE_VISION`` used to be defined here. Its ORIGIN moved to
# ``printer_incidents`` (2026-09-04) when the plate check became an incident KIND: the
# token is a PROJECTION of that row, so it belongs with the kind -> token table rather
# than in the correlation module that happened to raise the first one. The redundant
# alias above is the explicit re-export for the callers that still read it from here
# (``production_run``'s ``vision_hold``); nothing in THIS module reads it any more,
# because the reaction it belonged to moved to ``pause_recovery``.

# Verdicts where the finish IS the dispatched unit — the caller updates that item's
# terminal status and runs farm_policy attribution for it. ``fallback`` is included
# (best-effort attribution of the sole printing item) but is deliberately NOT in
# AUTO_CLEAR_VERDICTS, so an un-id-confirmed finish never auto-clears the gate.
ATTRIBUTED_VERDICTS: frozenset[str] = frozenset({"matched", "matched_by_name", "fallback"})
# Verdicts trusted to arm the identity cooldown auto-clear watch (id- or name-confirmed).
AUTO_CLEAR_VERDICTS: frozenset[str] = frozenset({"matched", "matched_by_name"})


@dataclass(frozen=True)
class TerminalResolution:
    """The outcome of correlating a terminal MQTT status to a queue item."""

    item: PrintQueueItem | None
    verdict: Verdict


def terminal_disposition(
    *,
    verdict: str,
    item_id: int | None,
    eject_profile_id: int | None,
    first_article: bool,
    batch_id: int | None,
    source_subtask_id: str | None,
    evidence: DepositEvidence,
    raise_gate: bool,
) -> TerminalDisposition:
    """Classify one terminal into the single value the occupancy authority consumes.

    The correlation rules live in this module, so the mapping from a VERDICT to "what
    happens to the plate next" lives here too — the authority stays free of the
    correlation lane and the terminal handler stays free of policy. It assembles, it
    does not decide: every input is already resolved by the caller.

    The policy ladder, in order:

    * an id- or name-confirmed finish of a farm unit that carries an eject profile and
      is not a first article → :class:`CooldownEject` on THAT unit. ``fallback`` is
      deliberately excluded (it is in ``ATTRIBUTED_VERDICTS`` but not in
      ``AUTO_CLEAR_VERDICTS``): a finish attributed only because it was the sole
      printing unit may not arm an automatic sweep;
    * a FIRST ARTICLE → :class:`EscalationOnly`. It carries a profile, but the part
      holds on the plate for inspection and the approval flow arms its own FA eject;
    * everything else — a foreign print, an unattributed deposit, a failure, a unit
      with no eject profile → :class:`EscalationOnly`, the never-armless floor. The
      foreign lane may UPGRADE that to :class:`ForeignAutoEject` afterwards, once its
      background identification proves the plate is the farm's own file; it is not
      decided here because the identification needs I/O this factory must not do.

    ``raise_gate`` is the caller's existing raise guard (the global
    ``require_plate_clear`` toggle, or farm involvement), carried through so a non-farm
    terminal on a toggle-off install still raises nothing — and so the authority never
    has to know what that guard is made of.
    """
    policy: OccupancyPolicy
    if verdict in AUTO_CLEAR_VERDICTS and item_id is not None and eject_profile_id is not None and not first_article:
        policy = CooldownEject(unit_id=item_id, run_id=batch_id)
    else:
        policy = EscalationOnly()
    return TerminalDisposition(
        queue_item_id=item_id,
        source_subtask_id=source_subtask_id,
        evidence=evidence,
        policy=policy,
        raise_gate=raise_gate,
    )


def upgrade_to_foreign_auto_eject(printer_id: int, profile_id: int, threshold_c: float) -> bool:
    """Swap a foreign plate's escalation hold for an AUTO eject once it is identified.

    The foreign branch raises its gate SYNCHRONOUSLY under :class:`EscalationOnly` (a
    deposit must block dispatch NOW), then identifies the plate in the background —
    that identification opens archives and re-fetches donors over FTPS, which the
    terminal callback cannot wait on. When it proves the plate is the farm's OWN file,
    this promotes the policy and the driver swaps the escalation hold for the cooldown
    watch that will sweep it.

    Returns True when the promotion landed. False means the authority refused
    ``not_occupied`` — an operator cleared the plate while we were identifying it, and
    an auto-eject onto a plate somebody already emptied is exactly what must not happen.
    """
    return (
        plate_occupancy.set_policy(printer_id, ForeignAutoEject(profile_id=profile_id, threshold_c=threshold_c)) is None
    )


@dataclass(frozen=True)
class DispatchDonor:
    """The on-disk source ``.gcode.3mf`` a farm dispatch prints FROM, and its plate.

    "Donor" is the fork's word for the source file a derived artefact is built from
    (``eject.donor.DonorSource.path``). Here it answers the question every
    consumer of a farm print asks in a slightly different way — the archive capture
    ("which 3MF do I attach?"), the eject builder ("which 3MF do I repack?") and the
    usage tracker ("which slice_info do I charge from?").

    ``local_path`` is BORROWED — it is the library file or the archive copy, never a
    temp — so no consumer may delete or truncate it (the 2026-08-15 durable-file-loss
    class). ``plate_id`` is the plate the FARM dispatched, which is authoritative
    over anything parsed out of the file or echoed by the printer: a hand-spliced
    ladder plate can declare a different index internally and still be the file that
    ran.
    """

    local_path: Path
    filename: str
    plate_id: int | None
    item_id: int


def _payload_names(payload: dict) -> set[str]:
    """The print-identity keys the terminal payload carries (subtask_name + filename).

    Keyed by :func:`print_identity_key` — the ONE "is this the same print?" key,
    shared with the foreign auto-eject identity check so the two answers cannot
    drift. A non-``str`` payload value is skipped rather than crashing the
    correlation of a real terminal event."""
    names: set[str] = set()
    for key in ("subtask_name", "filename"):
        raw = payload.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        normalized = print_identity_key(raw)
        if normalized:
            names.add(normalized)
    return names


async def _item_names(db: AsyncSession, item: PrintQueueItem) -> set[str]:
    """Normalized source names for a queue item — the names it could have been
    dispatched under (archive print_name/filename, library filename). Loaded lazily,
    only when id matching has already failed, so the happy path pays nothing."""
    names: set[str] = set()
    if item.archive_id is not None:
        from backend.app.models.archive import PrintArchive

        archive = await db.get(PrintArchive, item.archive_id)
        if archive is not None:
            for raw in (archive.print_name, archive.filename):
                if raw:
                    candidate = print_identity_key(raw)
                    if candidate:
                        names.add(candidate)
    if item.library_file_id is not None:
        from backend.app.models.library import LibraryFile

        library_file = await db.get(LibraryFile, item.library_file_id)
        if library_file is not None and library_file.filename:
            candidate = print_identity_key(library_file.filename)
            if candidate:
                names.add(candidate)
    return names


async def resolve_terminal_item(db: AsyncSession, printer_id: int, payload: dict) -> TerminalResolution:
    """Correlate a terminal MQTT status to the queue item that produced it.

    Candidates are the queue items on ``printer_id`` currently in ``printing``
    status (most-recently-started first). Matching order: subtask_id equality →
    dispatched-name match → single no-id candidate (fallback) → id-present-but-no-
    match / zero-candidate-with-id (foreign) → nothing printing, no id (none). See
    the module docstring for the full verdict semantics.
    """
    result = await db.execute(
        select(PrintQueueItem)
        .where(PrintQueueItem.printer_id == printer_id)
        .where(PrintQueueItem.status == "printing")
        .order_by(PrintQueueItem.started_at.desc())
    )
    candidates = list(result.scalars().all())
    payload_subtask = (payload.get("subtask_id") or "").strip() or None

    if not candidates:
        # Zero printing candidates. A terminal that STILL echoes a subtask_id is a
        # deposit Bambuddy did not dispatch onto this printer (the production S4 case:
        # every farm unit was cancelled, then the operator re-started the farm's own
        # USB file from the touchscreen — a fresh firmware-minted id, no printing row
        # left). That is FOREIGN, not silent "none": the caller must gate + watch +
        # alert, never raise the gate silently and strand the printer out of rotation.
        # With NO id echoed we cannot distinguish a real deposit from a bare state
        # blip, so keep "none" and never guess a foreign deposit into existence.
        if payload_subtask is not None:
            logger.warning(
                "farm_correlation: printer %s terminal subtask_id %r with ZERO printing queue items "
                "(name=%r) — FOREIGN; farm queue left untouched",
                printer_id,
                payload_subtask,
                payload.get("subtask_name") or payload.get("filename"),
            )
            return TerminalResolution(None, "foreign")
        return TerminalResolution(None, "none")

    # (1) subtask_id equality — the id Bambuddy minted for this exact dispatch.
    if payload_subtask:
        for item in candidates:
            if item.dispatch_subtask_id and item.dispatch_subtask_id == payload_subtask:
                return TerminalResolution(item, "matched")

    # (2) dispatched-name match — rescue path ONLY for items with no stamped
    # dispatch_subtask_id (legacy/pre-migration rows dispatched before stamping
    # existed). A stamped item can only be claimed by id equality (step 1): a
    # present-but-different payload id means "not this item" regardless of name —
    # an operator re-printing the SAME file locally mints a fresh id with an
    # identical name, which must resolve foreign, not matched_by_name (S4/S9).
    payload_names = _payload_names(payload)
    if payload_names:
        for item in candidates:
            if item.dispatch_subtask_id is None and payload_names & await _item_names(db, item):
                return TerminalResolution(item, "matched_by_name")

    # (3) fallback — the terminal carried no subtask_id at all and exactly one item
    # is printing. The sole printing unit is the best attribution; it is NOT id-
    # confirmed, so the caller updates it but does not arm auto-clear from it.
    if payload_subtask is None and len(candidates) == 1:
        logger.warning(
            "farm_correlation: printer %s terminal has no subtask_id; attributing the sole printing item %s "
            "by fallback (dispatch_subtask_id=%r). Not id-confirmed — verify after the next dispatch.",
            printer_id,
            candidates[0].id,
            candidates[0].dispatch_subtask_id,
        )
        return TerminalResolution(candidates[0], "fallback")

    # (4) foreign — the terminal named a subtask_id that matches no printing item.
    # The printer ran a job Bambuddy did not dispatch; farm state stays untouched.
    if payload_subtask is not None:
        logger.warning(
            "farm_correlation: printer %s terminal subtask_id %r matches no printing queue item "
            "(%d candidate(s)) — FOREIGN; farm queue left untouched",
            printer_id,
            payload_subtask,
            len(candidates),
        )
        return TerminalResolution(None, "foreign")

    # No id, multiple candidates, no name match — genuinely ambiguous (a
    # pathological >1-printing state). Attribute nothing rather than guess.
    logger.warning(
        "farm_correlation: printer %s terminal has no subtask_id and %d printing candidates with no name match "
        "— unresolved; farm queue left untouched",
        printer_id,
        len(candidates),
    )
    return TerminalResolution(None, "none")


async def resolve_printing_item(db: AsyncSession, printer_id: int, subtask_id: str | None) -> PrintQueueItem | None:
    """The queue item currently ``printing`` on ``printer_id``, or None.

    The ONE in-flight attribution used by the print-START lanes, mirroring
    :func:`resolve_terminal_item`'s identity discipline: when the printer echoes a
    ``subtask_id`` equal to a printing item's stamped ``dispatch_subtask_id`` that
    item wins (the id Bambuddy minted for this exact dispatch). Otherwise the sole
    ``printing`` item on the printer is the best attribution — more than one
    un-id-matched printing item is genuinely ambiguous, and ambiguity attributes
    nothing rather than guessing.

    Separate from :func:`resolve_terminal_item` on purpose: this filters
    ``status == "printing"`` (right at print start, wrong at completion, where the
    scheduler has already stamped a terminal status — see
    ``usage_tracker._resolve_run_context``) and it draws no ``foreign``/``none``
    verdict, because a print start has nothing to protect farm state from yet.
    """
    result = await db.execute(
        select(PrintQueueItem)
        .where(PrintQueueItem.printer_id == printer_id)
        .where(PrintQueueItem.status == "printing")
        .order_by(PrintQueueItem.started_at.desc())
    )
    candidates = list(result.scalars().all())
    if not candidates:
        return None

    subtask = (subtask_id or "").strip() or None
    if subtask:
        for item in candidates:
            if item.dispatch_subtask_id and item.dispatch_subtask_id == subtask:
                return item

    # No id match: the sole printing unit is the best attribution. More than one
    # un-id-matched printing item is genuinely ambiguous — attribute nothing.
    if len(candidates) == 1:
        return candidates[0]
    return None


async def resolve_active_plate_id(db: AsyncSession, printer_id: int, subtask_id: str | None) -> int | None:
    """Return the ``plate_id`` of the queue item currently printing on ``printer_id``.

    Used at print-start / archive-creation to scope the 3MF parse to the plate that
    actually ran (#1697): a multi-plate ``.gcode.3mf`` carries every plate's
    prediction + weight, so without the printed plate's index the archive would
    store the summed-across-plates totals for a single-plate print.

    Attribution is :func:`resolve_printing_item`'s (one origin). Returns that item's
    ``plate_id`` — which may itself be ``None`` for a non-plate-scoped (single-plate
    / non-farm) print — or ``None`` when nothing is printing or the printer runs more
    than one un-id-matched job.
    """
    item = await resolve_printing_item(db, printer_id, subtask_id)
    return item.plate_id if item is not None else None


async def resolve_item_donor(db: AsyncSession, item: PrintQueueItem) -> DispatchDonor | None:
    """The on-disk source ``.gcode.3mf`` ``item`` printed from, or None.

    THE one origin for "which file did this unit actually print, and which plate of
    it". Two rows can answer, in this order:

    * ``library_file_id`` — the dispatched source, present for the whole run's
      lifetime and the file the scheduler uploaded (``print_scheduler`` resolves the
      same pair at dispatch; G-code injection happens on a system temp AFTER this
      file is read, so the durable copy is always the un-injected original);
    * ``archive_id`` — the per-dispatch archive copy, which is what remains when a
      transient Direct-Print library row was reaped after dispatch
      (``cleanup_library_after_dispatch`` nulls ``library_file_id`` and rebinds the
      item to the archive).

    Both are checked with ``is_file()`` rather than ``exists()``: an archive row
    created without a 3MF carries ``file_path == ""``, and ``base_dir / ""`` is the
    base directory itself — which ``exists()`` happily confirms, handing the caller a
    DIRECTORY as a donor.

    Returns None when neither row resolves to bytes on disk; the caller decides
    whether that is a 409, a fall-back-to-guessing, or a wait (the missing-library
    file WAIT of 2026-08-15).
    """
    from backend.app.core.config import settings as app_settings
    from backend.app.models.archive import PrintArchive
    from backend.app.models.library import LibraryFile

    if item.library_file_id:
        library_file = await db.get(LibraryFile, item.library_file_id)
        if library_file is not None and library_file.file_path:
            lib_path = Path(library_file.file_path)
            path = lib_path if lib_path.is_absolute() else app_settings.base_dir / library_file.file_path
            if path.is_file():
                return DispatchDonor(
                    local_path=path,
                    filename=library_file.filename or path.name,
                    plate_id=item.plate_id,
                    item_id=item.id,
                )

    if item.archive_id:
        archive = await db.get(PrintArchive, item.archive_id)
        if archive is not None and archive.file_path:
            path = app_settings.base_dir / archive.file_path
            if path.is_file():
                return DispatchDonor(
                    local_path=path,
                    filename=archive.filename or path.name,
                    plate_id=item.plate_id,
                    item_id=item.id,
                )

    return None


async def resolve_dispatch_donor(db: AsyncSession, printer_id: int, subtask_id: str | None) -> DispatchDonor | None:
    """The donor of the print STARTING on ``printer_id``, when the farm dispatched it.

    A farm dispatch never has to guess which file it is printing: the queue item
    records the source row and the plate. Handing that to
    ``foreign_archive.locate_3mf_for_print`` turns the print-start capture from a
    name-guess-and-verify into a lookup, which is what closes the 2026-08-18
    zero-gram bleed — see that function's ``known_donor`` docstring for the incident.

    Returns None for every print the farm did NOT dispatch (a Bambu Studio / LAN
    job), which is exactly when the guessing lane is the right one. A donor row
    whose bytes are gone from disk also answers None, so a deleted library file
    degrades to guessing rather than to a hard failure.

    **ID-CONFIRMED ONLY, and that is a deliberate narrowing of
    :func:`resolve_printing_item`'s verdict.** That function's sole-printing-item
    fallback is right for scoping a plate — the cost of getting it wrong is a
    mis-summed weight on one archive row — but this answer decides WHICH FILE a
    print is recorded as having run, and the farm item sitting in ``printing`` on a
    printer is not always the job that just started (a screen-started print on a
    printer whose farm unit has not gone terminal yet). Supplying a donor there
    would archive the farm's file against somebody else's print. So the echoed
    ``subtask_id`` must equal the id Bambuddy minted for this exact dispatch, which
    the scheduler commits synchronously the moment ``start_print`` is accepted —
    long before the printer can echo a status back.

    A degenerate echo (empty ``subtask_id`` — a screen RESTART, 2026-07-21) is
    therefore refused rather than assumed. That mirrors the standing split in the
    foreign-eject lanes: the MANUAL flow may assume the printer's last farm item
    behind a human confirmation, while the AUTOMATIC paths stay fail-closed. This is
    an automatic path.
    """
    subtask = (subtask_id or "").strip()
    if not subtask:
        return None
    item = await resolve_printing_item(db, printer_id, subtask)
    if item is None or item.dispatch_subtask_id != subtask:
        return None
    donor = await resolve_item_donor(db, item)
    if donor is None:
        logger.info(
            "[DONOR] printer %s: queue item %s claims this print but neither its library file nor its "
            "archive copy is on disk — falling back to name-derived 3MF lookup",
            printer_id,
            item.id,
        )
    return donor


def classify_stop(
    payload: dict,
    printer_id: int,
    user_stopped_printer_ids: set[int],
    *,
    item_stop_source: str | None = None,
) -> StopVerdict | None:
    """THE classifier of "why did this terminal happen" — a closed set of verdicts.

    In precedence order:

    - ``farm_vision_abort`` — the ``printing`` row ALREADY carries the farm's own
      mark, written by ``pause_recovery.on_plate_vision_trip`` BEFORE it sent the
      stop. Highest precedence, above BOTH operator signals.
    - ``operator_ui``       — ``printer_id`` is in ``user_stopped_printer_ids`` (Stop
      was pressed in the Bambuddy queue UI).
    - ``operator_screen``   — the payload carries ``user_cancel_observed`` True: the
      firmware emitted a cancel-echo HMS code, i.e. the operator stopped the print
      on the printer's own touchscreen.
    - ``None``              — none of them (a genuine failure, a normal finish, or a
      reconcile-synthesised interruption that carries no echo/membership).

    The farm mark outranks the ECHO for a reason the design cannot afford to guess at:
    an MQTT ``print.stop`` plausibly produces the same cancel-echo a touchscreen stop
    does — unmeasured, so the mark must not depend on its absence — and relabelling the
    farm's own abort ``operator_screen`` routes the unit into the operator-stop
    disposition (cancelled, run holds, top-up on RESUME) instead of the requeue the
    abort exists to produce. It outranks UI MEMBERSHIP too, which is the older rule
    ("membership WINS") narrowed by one case: a human pressing Stop on a print the farm
    is already aborting has not changed why the print ended, and the mark is on disk
    while the membership is a process set.

    CAVEAT (observed live 2026-07-12, 007-H2C): H2C firmware emitted NO cancel-echo
    HMS on a touchscreen stop, so an H2C screen stop classifies as ``None`` — i.e.
    a genuine failure that feeds retry + quarantine accounting. Pending a deliberate
    wire-capture session hunting an alternative echo code on this firmware line,
    prefer stopping H2C farm units from the Bambuddy UI (membership wins).

    Pure — no DB, no I/O — so it is directly unit-testable and callable before the
    ``_user_stopped_printers`` set is mutated by the surrounding handler. The mark is
    read for it by :func:`printing_stop_mark`, at the one call site, so the verdict is
    decided ONCE and ahead of every status rewrite that keys off it.
    """
    if item_stop_source == STOP_SOURCE_FARM_VISION_ABORT:
        return STOP_SOURCE_FARM_VISION_ABORT
    if printer_id in user_stopped_printer_ids:
        return "operator_ui"
    if payload.get("user_cancel_observed"):
        return "operator_screen"
    return None


async def printing_stop_mark(db: AsyncSession, printer_id: int) -> str | None:
    """The ``stop_source`` already stamped on whatever is ``printing`` on this printer.

    The farm stamps its abort mark on the row BEFORE it sends the stop, so by the time
    the terminal arrives the mark is on disk and this read is what makes it visible to
    :func:`classify_stop`. Deliberately a bare read of the ``printing`` row rather than
    a re-use of the terminal correlation: the verdict is needed AHEAD of that
    correlation (every status rewrite keys off it), and "what did the farm already
    decide about the job this printer is running" needs no identity matching to answer.

    Newest first, and the first non-NULL mark wins — more than one ``printing`` row on
    a printer is the ambiguous shape ``resolve_printing_item`` also refuses to guess
    at, and a mark on any of them says the farm aborted this printer's job.
    """
    result = await db.execute(
        select(PrintQueueItem.stop_source)
        .where(PrintQueueItem.printer_id == printer_id)
        .where(PrintQueueItem.status == "printing")
        .order_by(PrintQueueItem.started_at.desc())
    )
    for mark in result.scalars().all():
        if mark:
            return mark
    return None


async def resolve_printing_farm_item(db: AsyncSession, printer_id: int) -> PrintQueueItem | None:
    """The FARM unit currently ``printing`` on ``printer_id``, or None.

    "Farm" here is the loop's own test, unchanged since Phase 3.3: the item carries an
    ``eject_profile_id``, or its batch carries a ``sku_file_id``. Distinct from
    :func:`resolve_printing_item`, which asks the IDENTITY question (which unit is this
    echoed job?) — this one asks the OWNERSHIP question (is the farm loop responsible
    for what is on this printer?), which is what decides whether there is a row to
    stamp and a unit to requeue.

    Extracted from the deleted ``on_native_plate_detection`` when the plate-vision
    reaction moved to ``pause_recovery``: the resolution was the reusable half of that
    function, and correlation is where it belongs.
    """
    result = await db.execute(
        select(PrintQueueItem)
        .where(PrintQueueItem.printer_id == printer_id)
        .where(PrintQueueItem.status == "printing")
        .order_by(PrintQueueItem.started_at.desc())
    )
    for candidate in result.scalars().all():
        if candidate.eject_profile_id is not None:
            return candidate
        if candidate.batch_id is not None:
            batch = await db.get(PrintBatch, candidate.batch_id)
            if batch is not None and batch.sku_file_id is not None:
                return candidate
    return None


async def farm_work_targets_printer(db: AsyncSession, printer_id: int) -> bool:
    """True if any farm queue item (pending or printing, belonging to a batch with a
    ``sku_file_id``) is bound to ``printer_id``.

    Drives the Phase-1 plate-gate raise condition (a printer that farm work targets
    must gate on plate-clear regardless of the global convenience toggle) and, when
    inverted, the startup hygiene that clears stale gates on non-farm printers.
    """
    result = await db.execute(
        select(PrintQueueItem.id)
        .join(PrintBatch, PrintQueueItem.batch_id == PrintBatch.id)
        .where(PrintQueueItem.printer_id == printer_id)
        .where(PrintQueueItem.status.in_(("pending", "printing")))
        .where(PrintBatch.sku_file_id.is_not(None))
        .limit(1)
    )
    return result.first() is not None


async def farm_work_slated_for(db: AsyncSession, *, printer_id: int, printer_model: str | None) -> bool:
    """True if ANY farm work could next occupy this printer — bound OR pooled.

    Two halves. The bound half is :func:`farm_work_targets_printer` verbatim. The
    pool half asks the wider question that function must never ask: is there a
    pending farm item with ``printer_id IS NULL`` whose POOL target — a model, or a
    printer subset — this printer is a member of, i.e. work the scheduler may land
    here on its next tick.

    ``farm_work_targets_printer`` deliberately stays its own function: it drives
    PLATE-GATE semantics, where "bound work" is the whole question, and widening it
    to pooled work would raise a gate on every printer a pool merely touches.

    Membership is decided by :meth:`DispatchTarget.matches` — the same predicate the
    scheduler uses (``print_scheduler._find_idle_printer_for_target``) — over ONE
    query of the DISTINCT ``(target_model, target_printer_ids)`` pairs, evaluated in
    Python. The pairs are few (one per live run target) and the model comparison has
    no SQL-side spelling, so one answer serves both dialects and cannot drift from
    the scheduler's.

    Consumer: the idle deep-park (``farm_policy._maybe_idle_deep_park``). The park is
    cosmetic, so an over-TRUE answer costs nothing but a skipped park; a false
    NEGATIVE parks a bed the scheduler is about to print on.
    """
    if await farm_work_targets_printer(db, printer_id):
        return True

    result = await db.execute(
        select(PrintQueueItem.target_model, PrintQueueItem.target_printer_ids)
        .join(PrintBatch, PrintQueueItem.batch_id == PrintBatch.id)
        .where(PrintQueueItem.printer_id.is_(None))
        .where(PrintQueueItem.status == "pending")
        .where(or_(PrintQueueItem.target_model.is_not(None), PrintQueueItem.target_printer_ids.is_not(None)))
        .where(PrintBatch.sku_file_id.is_not(None))
        .distinct()
    )
    for target_model, target_printer_ids in result.all():
        # ``target_of`` reads exactly three attributes (its ``HasTargetColumns``
        # protocol), so the column pair is a complete argument — no row needed.
        target = target_of(
            SimpleNamespace(printer_id=None, target_model=target_model, target_printer_ids=target_printer_ids)
        )
        if target.matches(printer_id, printer_model):
            return True
    return False
