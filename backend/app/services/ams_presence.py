"""Mid-run AMS refill recognition (presence + terminal RFID re-read).

Single owner of AMS presence-transition policy. Everything downstream of
``main.on_ams_change`` already handles refills (RFID auto-create/assign, reused-
tag respool, tagless auto-mint/auto-config in ``services.spool_tagless``, low-
spool staged-unit auto-release), but that pipeline only fires when the AMS
change-hash changes. This module closes the gap the hash alone cannot see — a
spool inserted while idle or mid-print that the firmware never auto-reads:

* :func:`on_ams_change` — presence-transition tracking, called from
  ``main.on_ams_change`` inside the existing per-printer lock. On a presence GAIN
  while the printer is idle it fires an immediate per-slot RFID re-read so a Bambu
  spool resolves via the normal tag path within seconds. It NEVER prompts: a
  tagless spool is now silently minted/configured by ``services.spool_tagless``
  (there is no more ``new_spool_detected`` event). A presence LOSS only updates
  the last-presence map — NO silent auto-unassign (a spool pulled for drying keeps
  its assignment and gram history).

* :func:`on_printer_terminal` — the auto RFID re-read sweep, called from
  ``main.on_print_complete`` (skipped for eject-job terminals). When a print ends
  it re-reads every eligible unidentified slot so a mid-print refill is recognized
  within seconds. A slot bound to an auto-minted tagless spool (``data_origin ==
  "ams_auto"``) IS swept — so a Bambu roll swapped into it mid-print gets
  identified at print end — while an operator-bound slot stays protected. Results
  flow the normal RFID pipeline; this module does not duplicate it.

Presence is POSITIVE-evidence-only: ``state ∈ {10, 11}`` is present; state 9,
None, and unknown dialect codes (H2C idle empties report ``state=0``) all read
absent, so an H2C never reads as phantom spools.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.services.printer_manager import printer_manager
from backend.app.services.spool_tag_matcher import is_valid_tag

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Spacing between sequential per-slot RFID re-reads in the terminal sweep — a
# read takes a few seconds and the AMS can only identify one slot at a time.
_RFID_REREAD_SPACING_S = 5

# ``ams_status_main == 2`` means the AMS is actively identifying a tag (used to
# early-skip the inter-read spacing wait once a read has completed).
_AMS_STATUS_IDENTIFYING = 2

# --- Module-level edge state (matches the fork's other event-edge bookkeeping,
#     e.g. farm_staging._tray_signatures). Lost on restart; startup priming and
#     the first-push seeding tolerate that. -----------------------------------

# (printer_id, ams_id, tray_id) -> last observed physical presence (bool).
_last_presence: dict[tuple[int, int, int], bool] = {}

# Printers whose first on_ams_change (post-restart) has been processed. The first
# push only seeds the presence map (no re-read); later pushes act on gains.
_primed: set[int] = set()

# printer_id -> subtask_id already swept at its terminal. Dedupes duplicate
# on_print_complete callbacks for the same print (one-shot per RUNNING→terminal).
_swept_subtasks: dict[int, str] = {}


def _reset_state() -> None:
    """Test hook: clear all module-level edge state between cases."""
    _last_presence.clear()
    _primed.clear()
    _swept_subtasks.clear()


# --- Tray / state predicates ----------------------------------------------


def _norm_state(raw: object) -> int | None:
    """Normalize a tray ``state`` (may arrive as int or str) to int or None."""
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _tray_present(tray: dict) -> bool:
    """Positive-evidence-only presence: seated/loaded (state 10/11) only."""
    return _norm_state(tray.get("state")) in (10, 11)


def _printer_running(state) -> bool:
    return state is not None and getattr(state, "state", None) in ("RUNNING", "PAUSE")


def _iter_ams_units(state) -> list:
    """Yield the AMS unit dicts from a printer state's merged raw_data."""
    if state is None:
        return []
    raw = getattr(state, "raw_data", None) or {}
    ams = raw.get("ams", [])
    if isinstance(ams, dict):
        ams = ams.get("ams", [])
    return ams if isinstance(ams, list) else []


# --- Assignment context ----------------------------------------------------


async def _spoolman_active(db: AsyncSession) -> bool:
    from backend.app.api.routes.settings import get_setting

    val = await get_setting(db, "spoolman_enabled")
    return bool(val) and val.lower() == "true"


async def _slot_assignment_context(
    db: AsyncSession, printer_id: int, ams_id: int, tray_id: int, spoolman_active: bool
) -> tuple[bool, bool, str | None]:
    """Resolve (has_assignment, assigned_spool_spent, spool_data_origin) for a slot.

    The internal ``SpoolAssignment`` is the source of truth; when Spoolman mode is
    active a ``SpoolmanSlotAssignment`` also counts as an assignment (Spoolman does
    not track ``spent_at`` or ``data_origin``, so both trailing fields are
    False/None). ``data_origin`` lets the terminal sweep tell an auto-minted
    tagless slot (sweepable) from an operator-bound one (protected).
    """
    from backend.app.models.spool import Spool  # noqa: F401 — selectinload target
    from backend.app.models.spool_assignment import SpoolAssignment

    res = await db.execute(
        select(SpoolAssignment)
        .options(selectinload(SpoolAssignment.spool))
        .where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    sa = res.scalar_one_or_none()
    if sa is not None:
        spent = sa.spool is not None and getattr(sa.spool, "spent_at", None) is not None
        origin = getattr(sa.spool, "data_origin", None) if sa.spool is not None else None
        return True, spent, origin

    if spoolman_active:
        from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

        res2 = await db.execute(
            select(SpoolmanSlotAssignment.id).where(
                SpoolmanSlotAssignment.printer_id == printer_id,
                SpoolmanSlotAssignment.ams_id == ams_id,
                SpoolmanSlotAssignment.tray_id == tray_id,
            )
        )
        if res2.first() is not None:
            return True, False, None

    return False, False, None


# --- presence-gain RFID re-read -------------------------------------------


async def on_ams_change(printer_id: int, ams_data: list, db: AsyncSession) -> None:
    """Track presence transitions for a printer's AMS trays.

    Called from ``main.on_ams_change`` inside the per-printer assignment lock with
    the merged tray state and an open session. On a presence GAIN while the
    printer is idle it fires an immediate per-slot RFID re-read (so a Bambu spool
    resolves via the tag path fast; mid-print refills are handled by the terminal
    sweep). Never raises — a farm-side failure must never break the AMS callback
    chain.
    """
    try:
        is_first = printer_id not in _primed
        _primed.add(printer_id)

        state = printer_manager.get_status(printer_id)
        running = _printer_running(state)

        for ams_unit in ams_data or []:
            if not isinstance(ams_unit, dict):
                continue
            try:
                ams_id = int(ams_unit.get("id", 0))
            except (TypeError, ValueError):
                continue
            for tray in ams_unit.get("tray", []) or []:
                if not isinstance(tray, dict):
                    continue
                try:
                    tray_id = int(tray.get("id", 0))
                except (TypeError, ValueError):
                    continue

                key = (printer_id, ams_id, tray_id)
                present = _tray_present(tray)
                prev = _last_presence.get(key)
                _last_presence[key] = present

                if is_first:
                    # First push after a (re)start only seeds the presence map so
                    # a refill done while down doesn't read as a fresh gain.
                    continue

                # Steady state: act only on a presence GAIN, and only while the
                # printer is idle. Firing ams_get_rfid during a print is unsafe;
                # the terminal sweep handles mid-print refills. A LOSS only updates
                # the map above (NO auto-unassign).
                if present and not prev and not running:
                    client = printer_manager.get_client(printer_id)
                    if client is not None:
                        try:
                            client.ams_refresh_tray(ams_id, tray_id)
                        except Exception:  # noqa: BLE001 — best-effort re-read
                            logger.exception(
                                "AMS presence: immediate re-read failed for printer %d AMS%d-T%d",
                                printer_id,
                                ams_id,
                                tray_id,
                            )
    except Exception:  # noqa: BLE001 — must never crash the AMS callback chain
        logger.exception("AMS presence tracking failed for printer %s", printer_id)


# --- terminal RFID re-read sweep -------------------------------------------


async def on_printer_terminal(printer_id: int) -> None:
    """Re-read unidentified AMS slots when a print reaches a terminal state.

    Called from ``main.on_print_complete`` (skipped for eject-job terminals so
    each unit cycle sweeps once at the PRINT terminal, not again at the eject
    terminal). One-shot per RUNNING/PAUSE→terminal transition; sequential with
    spacing. Never raises. Results flow the normal RFID pipeline.
    """
    try:
        # Dedup duplicate terminal callbacks: on_print_complete can fire several
        # times for one ending. Key on the print's subtask_id (unique per
        # dispatch). The get/set is synchronous (no await between) so racing
        # create_task()d sweeps for the same terminal collapse to one.
        state = printer_manager.get_status(printer_id)
        subtask = (getattr(state, "subtask_id", None) or "") if state is not None else ""
        if _swept_subtasks.get(printer_id) == subtask:
            return
        _swept_subtasks[printer_id] = subtask

        client = printer_manager.get_client(printer_id)
        if state is None or client is None:
            return

        from backend.app.core.database import async_session

        eligible: list[tuple[int, int]] = []
        async with async_session() as db:
            spoolman_active = await _spoolman_active(db)
            for ams_unit in _iter_ams_units(state):
                if not isinstance(ams_unit, dict):
                    continue
                try:
                    ams_id = int(ams_unit.get("id", 0))
                except (TypeError, ValueError):
                    continue
                for tray in ams_unit.get("tray", []) or []:
                    if not isinstance(tray, dict):
                        continue
                    try:
                        tray_id = int(tray.get("id", 0))
                    except (TypeError, ValueError):
                        continue
                    # state 9/10/11 eligible — state 9 INCLUDED because a mid-print
                    # refill sometimes stays state=9 until re-read. state 0/None
                    # (unknown dialect / no data) EXCLUDED.
                    if _norm_state(tray.get("state")) not in (9, 10, 11):
                        continue
                    if is_valid_tag(tray.get("tag_uid", "") or "", tray.get("tray_uuid", "") or ""):
                        continue  # already identified — nothing to re-read
                    has_assignment, _spent, origin = await _slot_assignment_context(
                        db, printer_id, ams_id, tray_id, spoolman_active
                    )
                    if has_assignment and origin != "ams_auto":
                        # An operator-bound slot encodes deliberate intent — don't
                        # churn a manual third-party setup. An auto-minted tagless
                        # slot (origin == "ams_auto") IS swept, so a Bambu roll
                        # swapped into it mid-print is identified at print end.
                        continue
                    eligible.append((ams_id, tray_id))

        if not eligible:
            return

        logger.info("[Printer %s] terminal RFID re-read sweep: %d unidentified slot(s)", printer_id, len(eligible))
        for idx, (ams_id, tray_id) in enumerate(eligible):
            try:
                client.ams_refresh_tray(ams_id, tray_id)
                logger.info("[Printer %s] terminal RFID re-read: AMS%d slot%d", printer_id, ams_id, tray_id)
            except Exception:  # noqa: BLE001 — one failed read must not stop the sweep
                logger.exception("[Printer %s] terminal RFID re-read failed: AMS%d slot%d", printer_id, ams_id, tray_id)
            if idx < len(eligible) - 1:
                await _spacing_wait(printer_id)
    except Exception:  # noqa: BLE001 — the sweep must never crash the completion callback
        logger.exception("AMS terminal RFID re-read sweep failed for printer %s", printer_id)


async def _spacing_wait(printer_id: int) -> None:
    """Wait up to ``_RFID_REREAD_SPACING_S`` between reads.

    Best-effort early exit: once we have observed the AMS actively identifying
    (``ams_status_main == 2``) and then return to idle, the previous read has
    completed, so move to the next slot without burning the full window.
    """
    elapsed = 0.0
    step = 0.5
    saw_identifying = False
    while elapsed < _RFID_REREAD_SPACING_S:
        await asyncio.sleep(step)
        elapsed += step
        state = printer_manager.get_status(printer_id)
        main = getattr(state, "ams_status_main", 0) if state is not None else 0
        if main == _AMS_STATUS_IDENTIFYING:
            saw_identifying = True
        elif saw_identifying:
            break
