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
                         payload's ``subtask_name`` and ``filename`` are
                         basename-stripped of the ``.gcode.3mf`` / ``.3mf`` /
                         ``.gcode`` extension and lower-cased, and must intersect the
                         same normalization of the item's ``archive.print_name`` /
                         ``archive.filename`` / ``library_file.filename``.
- ``fallback``         — the terminal carried no ``subtask_id`` at all (firmware
                         that resets it on cancel, or an upgrade-day row dispatched
                         before ``dispatch_subtask_id`` existed) AND there is exactly
                         one printing item. The sole printing unit on the printer is
                         the best attribution; logged at WARNING because it was not
                         id-confirmed.
- ``foreign``          — the terminal carried a ``subtask_id`` that matches NO
                         printing item. The printer ran something Bambuddy did not
                         dispatch. Farm state MUST NOT be mutated: no unit is marked
                         done, no retry/quarantine is attributed, and no auto-clear
                         is armed. The caller still raises the plate-clear gate
                         (the deposit is real) but keys it human-clear-only.
- ``none``             — nothing is printing on this printer (non-farm/plain print,
                         or already reconciled). No queue item to touch.

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
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select

from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

Verdict = Literal["matched", "matched_by_name", "fallback", "foreign", "none"]

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


def _normalize_name(name: str | None) -> str | None:
    """Basename, extension-stripped, lower-cased — or None if empty.

    Strips a leading path (``/`` or ``\\``) and one trailing 3MF/gcode extension so
    a printer-reported ``subtask_name`` ("SKU007.01") and a stored filename
    ("SKU007.01.gcode.3mf") compare equal.
    """
    if not name:
        return None
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    lower = base.lower()
    for ext in (".gcode.3mf", ".3mf", ".gcode"):
        if lower.endswith(ext):
            lower = lower[: -len(ext)]
            break
    lower = lower.strip()
    return lower or None


def _payload_names(payload: dict) -> set[str]:
    """Normalized names the terminal payload carries (subtask_name + filename)."""
    names: set[str] = set()
    for key in ("subtask_name", "filename"):
        normalized = _normalize_name(payload.get(key))
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
            for candidate in (_normalize_name(archive.print_name), _normalize_name(archive.filename)):
                if candidate:
                    names.add(candidate)
    if item.library_file_id is not None:
        from backend.app.models.library import LibraryFile

        library_file = await db.get(LibraryFile, item.library_file_id)
        if library_file is not None:
            candidate = _normalize_name(library_file.filename)
            if candidate:
                names.add(candidate)
    return names


async def resolve_terminal_item(db: AsyncSession, printer_id: int, payload: dict) -> TerminalResolution:
    """Correlate a terminal MQTT status to the queue item that produced it.

    Candidates are the queue items on ``printer_id`` currently in ``printing``
    status (most-recently-started first). Matching order: subtask_id equality →
    dispatched-name match → single no-id candidate (fallback) → id-present-but-no-
    match (foreign) → nothing printing (none). See the module docstring for the
    full verdict semantics.
    """
    result = await db.execute(
        select(PrintQueueItem)
        .where(PrintQueueItem.printer_id == printer_id)
        .where(PrintQueueItem.status == "printing")
        .order_by(PrintQueueItem.started_at.desc())
    )
    candidates = list(result.scalars().all())
    if not candidates:
        return TerminalResolution(None, "none")

    payload_subtask = (payload.get("subtask_id") or "").strip() or None

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


def classify_stop(payload: dict, printer_id: int, user_stopped_printer_ids: set[int]) -> str | None:
    """Classify a terminal payload as an OPERATOR stop, or ``None`` (Phase 3.1).

    - ``operator_ui``     — ``printer_id`` is in ``user_stopped_printer_ids`` (Stop
      was pressed in the Bambuddy queue UI). Membership WINS over the screen echo.
    - ``operator_screen`` — the payload carries ``user_cancel_observed`` True: the
      firmware emitted a cancel-echo HMS code, i.e. the operator stopped the print
      on the printer's own touchscreen.
    - ``None``            — neither signal (a genuine failure, a normal finish, or a
      reconcile-synthesised interruption that carries no echo/membership).

    CAVEAT (observed live 2026-07-12, 007-H2C): H2C firmware emitted NO cancel-echo
    HMS on a touchscreen stop, so an H2C screen stop classifies as ``None`` — i.e.
    a genuine failure that feeds retry + quarantine accounting. Pending a deliberate
    wire-capture session hunting an alternative echo code on this firmware line,
    prefer stopping H2C farm units from the Bambuddy UI (membership wins).

    Pure — no DB, no I/O — so it is directly unit-testable and callable before the
    ``_user_stopped_printers`` set is mutated by the surrounding handler.
    """
    if printer_id in user_stopped_printer_ids:
        return "operator_ui"
    if payload.get("user_cancel_observed"):
        return "operator_screen"
    return None


async def on_native_plate_detection(db: AsyncSession, printer_id: int, short_codes: set[str]) -> bool:
    """React to the printer's NATIVE pre-print plate-occupancy vision check (Phase 3.3).

    ``short_codes`` are the just-arrived HMS codes in
    ``bambu_mqtt._HMS_PLATE_OCCUPANCY_CODES`` (foreign-objects-on-heatbed /
    plate-marker). The printer PAUSEs the job on such a code, which for the farm
    loop is authoritative eject-verification: a hit at unit N's start means unit
    N−1's sweep did not clear the plate. When a FARM unit is currently ``printing``
    on ``printer_id`` (its batch has a ``sku_file_id`` OR the item carries an
    ``eject_profile_id``):

      (a) raise a HUMAN-CLEAR-ONLY plate gate — ``source_subtask_id=None`` so the
          Phase-1 rearm/auto-clear rules never release it (only a human clearing
          the bed does);
      (b) flag the unit ``waiting_reason="plate_not_empty_printer_detected"`` (it
          stays ``printing`` — the job is PAUSEd on the printer; the operator clears
          the bed and Resumes on-screen, or stops it and the normal terminal path
          takes over);
      (c) fire ``on_plate_not_empty`` with the printer-vision ``source_detail``.

    No auto-retry and no quarantine mutation follow from the pause itself. Returns
    True if a farm unit was flagged; a non-farm printer yields no gate (False).
    """
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service
    from backend.app.services.printer_manager import printer_manager

    result = await db.execute(
        select(PrintQueueItem)
        .where(PrintQueueItem.printer_id == printer_id)
        .where(PrintQueueItem.status == "printing")
        .order_by(PrintQueueItem.started_at.desc())
    )
    item: PrintQueueItem | None = None
    for candidate in result.scalars().all():
        if candidate.eject_profile_id is not None:
            item = candidate
            break
        if candidate.batch_id is not None:
            batch = await db.get(PrintBatch, candidate.batch_id)
            if batch is not None and batch.sku_file_id is not None:
                item = candidate
                break
    if item is None:
        logger.info(
            "farm_correlation: printer %s native plate-occupancy %s but no farm unit printing — no gate",
            printer_id,
            sorted(short_codes),
        )
        return False

    # (a) human-clear-only gate, (b) flag the unit.
    printer_manager.set_awaiting_plate_clear(printer_id, True, source_subtask_id=None)
    item.waiting_reason = "plate_not_empty_printer_detected"
    await db.commit()

    # (c) source-disambiguated notification.
    printer = await db.get(Printer, printer_id)
    printer_name = printer.name if printer is not None else f"printer {printer_id}"
    detail = (
        "Printer vision detected foreign objects on the heatbed at job start — the previous unit's "
        "sweep may not have cleared the plate. Clear the bed, then Resume on the printer screen."
    )
    await notification_service.on_plate_not_empty(printer_id, printer_name, db, source_detail=detail)
    logger.warning(
        "farm_correlation: printer %s native plate-occupancy %s while unit %s printing — "
        "human-clear-only gate raised, unit flagged plate_not_empty_printer_detected",
        printer_id,
        sorted(short_codes),
        item.id,
    )
    return True


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
