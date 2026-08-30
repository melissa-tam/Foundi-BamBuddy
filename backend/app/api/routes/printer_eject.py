"""Manual "Eject plate" route.

A thin HTTP boundary over ``services.eject.manual.manual_eject``: the service decides
and returns ONE :class:`~backend.app.services.eject.manual.EjectVerdict`; this module
maps that verdict to an HTTP shape and owns every English sentence in the lane. The
service speaks only in closed tokens (the 2026-08-20 ``slot_recheck`` precedent), which
is what lets the frontend key i18n off the wire ``code`` instead of rendering backend
prose.

Kept in its own module (not the 159 KB ``printers.py``) per the fork's large-route-file
convention. Permission ``PRINTERS_CONTROL`` — an eject is motion, the same class as
stop/pause; no new permission for zero differentiation.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.schemas.printer import EjectOrigin, EjectRefusalReason
from backend.app.services.eject import remote as eject_remote
from backend.app.services.eject.manual import EjectVerdict, manual_eject

router = APIRouter(prefix="/printers", tags=["printers"])


#: One sentence per refusal reason. The service returns tokens; the English lives HERE
#: and nowhere else, so a reason can never reach an operator as two different sentences.
_REFUSAL_MESSAGES: dict[EjectRefusalReason, str] = {
    "job_active": "Printer is running a job; wait for it to finish or stop it, then eject",
    "dispatch_in_flight": "A queued unit is being sent to this printer; retry in a few seconds",
    "eject_in_flight": "An eject is already in flight on this printer",
    "not_connected": "Printer is not connected; cannot eject",
    "no_plate_gate": "Printer is not awaiting plate clear; nothing to eject",
    "bed_unreadable": "Live bed temperature is unavailable; wait a few seconds for printer telemetry and retry",
    "first_article": "First article awaiting approval; approve or reject it from the run",
    "no_donor": "No file available to build the eject — use Mark plate as cleared and remove the part by hand",
    "not_found": "Printer not found",
    "profile_not_found": "Eject profile not found",
}

#: The two reasons that are a MISSING RESOURCE rather than a conflicting state.
_NOT_FOUND_REASONS: frozenset[EjectRefusalReason] = frozenset({"not_found", "profile_not_found"})


def _needs_input_message(origin: EjectOrigin, print_name: str | None) -> str:
    """The sentence for a needs-input 409, per origin.

    The frontend renders its own i18n copy keyed on ``origin``; this is what an API
    client, a log line and a curl probe read.
    """
    if origin == "declared":
        return "Plate declared occupied — confirm the part height and eject profile to sweep it"
    if origin == "farm_unit":
        subject = print_name or "a farm unit this lane cannot sweep from its own record"
        return f"Plate holds {subject} — confirm the part height and eject profile to sweep it"
    return (
        "This plate was started outside the farm — confirm an eject profile to sweep it, "
        "or use Mark plate as cleared and remove the part by hand"
    )


def _http_error(verdict: EjectVerdict) -> HTTPException:
    """The ONE verdict → HTTPException map. Only non-success verdicts reach it."""
    if verdict.outcome == "needs_input":
        origin: EjectOrigin = verdict.origin or "foreign"
        # The wire code stays ``foreign_plate``: it is the dialog-opening contract the
        # frontend has always branched on, and ``origin`` is what widened underneath it.
        return HTTPException(
            status_code=409,
            detail={
                "code": "foreign_plate",
                "origin": origin,
                "message": _needs_input_message(origin, verdict.print_name),
                "print_name": verdict.print_name,
                "max_z_height_mm": verdict.max_z_height_mm,
                "suggested_eject_profile_id": verdict.suggested_eject_profile_id,
            },
        )

    if verdict.outcome == "bed_hot":
        bed_c = verdict.bed_c
        threshold_c = verdict.threshold_c
        return HTTPException(
            status_code=409,
            detail={
                "code": "bed_hot",
                "bed_c": bed_c,
                "threshold_c": threshold_c,
                "message": (
                    f"Bed is {bed_c:.1f}°C, above the {threshold_c:.1f}°C eject threshold — confirm to eject hot"
                ),
            },
        )

    reason = verdict.reason
    detail: dict = {"code": reason, "message": _REFUSAL_MESSAGES[reason]}
    if reason == "eject_in_flight":
        # Two different situations to an operator: a sweep the printer has STARTED (and
        # will finish on its own) and one it has not yet acknowledged.
        detail["started"] = bool(verdict.started)
        detail["age_s"] = verdict.age_s
    return HTTPException(status_code=404 if reason in _NOT_FOUND_REASONS else 409, detail=detail)


class EjectNowBody(BaseModel):
    """Body for ``POST /printers/{id}/eject``.

    ``allow_hot`` is the explicit hot-bed confirm — the UI re-calls with it True after
    the operator acknowledges the live-bed-vs-threshold dialog raised by a 409
    ``bed_hot``. ``eject_profile_id`` is the operator's chosen sweep profile; supplying
    it is what turns the confirm dialog's prompt into its dispatch.

    ``declare_occupied`` is the on-demand lane: the operator states a part is on a plate
    the farm never gated, so the service raises the gate itself instead of refusing
    ``no_plate_gate``. ``max_z_height_mm`` is the operator's confirmed part height — it
    supersedes the donor's parsed header (which is only the dialog's prefill) and is
    REQUIRED when the donor is a bare container. ``gt=0`` is the floor; the profile's
    ``max_part_height_mm`` guard remains the ceiling and surfaces as a 409."""

    allow_hot: bool = False
    eject_profile_id: int | None = None
    declare_occupied: bool = False
    max_z_height_mm: float | None = Field(default=None, gt=0)


@router.post("/{printer_id}/eject")
async def eject_now(
    printer_id: int,
    body: EjectNowBody | None = None,
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
    db: AsyncSession = Depends(get_db),
):
    """Sweep the plate on this printer — whatever put the part there.

    200 ``{mode, queue_item_id}`` when the sweep is on its way (``mode`` is
    ``released_watch`` when an armed cooldown watch was signalled, ``dispatched``
    otherwise).

    409 ``foreign_plate`` (with ``origin``/``print_name``/``max_z_height_mm``/
    ``suggested_eject_profile_id``) is NOT a failure: it is the eject asking for the part
    height and the sweep profile, and the UI re-calls with both to dispatch. 409
    ``bed_hot`` carries ``bed_c``/``threshold_c`` for the hot-bed confirm. Every other
    refusal carries a stable ``code`` plus an actionable ``message``; 404 for an unknown
    printer or eject profile.

    With ``declare_occupied`` true and NO gate raised, the service raises the plate gate
    itself (source-less ⇒ human-clear-only) before anything else, so the declaration
    revokes an in-flight dispatch lease instead of racing it. That raise stands even if
    the operator abandons the dialog — the plate is occupied either way, and "Mark plate
    as cleared" is the undo.
    """
    allow_hot = bool(body.allow_hot) if body is not None else False
    eject_profile_id = body.eject_profile_id if body is not None else None
    declare_occupied = bool(body.declare_occupied) if body is not None else False
    max_z_override = body.max_z_height_mm if body is not None else None
    try:
        verdict = await manual_eject(
            db,
            printer_id,
            allow_hot=allow_hot,
            eject_profile_id=eject_profile_id,
            declare_occupied=declare_occupied,
            max_z_override=max_z_override,
        )
    except eject_remote.EjectDispatchError as exc:
        # Infrastructure, not a verdict: the build failed, the upload failed, or the
        # printer refused the start. ``code`` carries the occupancy authority's own
        # refusal token when a claim lost a race during the upload, so the dialog
        # branches on the same vocabulary the state machine speaks.
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc

    if verdict.mode is not None:
        return {"mode": verdict.mode, "queue_item_id": verdict.queue_item_id}
    raise _http_error(verdict)
