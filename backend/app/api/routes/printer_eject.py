"""Manual "Eject now" route (W2).

A thin HTTP boundary over ``services.eject.manual.manual_eject``: it translates the
service's domain errors to ``HTTPException`` with actionable bodies and otherwise
carries no logic. Kept in its own module (not the 159 KB ``printers.py``) per the
fork's large-route-file convention. Permission ``PRINTERS_CONTROL`` — an eject is
motion, the same class as stop/pause; no new permission for zero differentiation.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.services.eject import remote as eject_remote
from backend.app.services.eject.manual import BedTooHot, ForeignPlateEject, ManualEjectError, manual_eject

router = APIRouter(prefix="/printers", tags=["printers"])


class EjectNowBody(BaseModel):
    """Body for ``POST /printers/{id}/eject``. ``allow_hot`` is the explicit hot-bed
    confirm — the UI re-calls with it True after the operator acknowledges the
    live-bed-vs-threshold dialog raised by a 409 ``bed_hot``. ``eject_profile_id`` is
    the operator's chosen profile for the foreign-plate confirm — the UI re-calls with
    it after a 409 ``foreign_plate`` prompt to dispatch the sweep."""

    allow_hot: bool = False
    eject_profile_id: int | None = None


@router.post("/{printer_id}/eject")
async def eject_now(
    printer_id: int,
    body: EjectNowBody | None = None,
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger the part-present eject sweep for a farm-known finished unit.

    404 unknown printer; 409 ``bed_hot`` (with ``bed_c``/``threshold_c``) when the
    bed is above the release threshold and ``allow_hot`` is false; 409 ``foreign_plate``
    (with ``print_name``/``max_z_height_mm``/``suggested_eject_profile_id``) when the
    gate came from a print the farm did not dispatch and no ``eject_profile_id`` was
    supplied — the UI re-calls with a chosen profile to sweep it; other 409s carry a
    stable ``code`` + actionable ``message``. On success returns the eject mode
    (``released_watch`` when an armed watch was signalled, ``dispatched`` otherwise).
    """
    allow_hot = bool(body.allow_hot) if body is not None else False
    eject_profile_id = body.eject_profile_id if body is not None else None
    try:
        return await manual_eject(db, printer_id, allow_hot=allow_hot, eject_profile_id=eject_profile_id)
    except ForeignPlateEject as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "foreign_plate",
                "message": str(exc),
                "print_name": exc.print_name,
                "max_z_height_mm": exc.max_z_height_mm,
                "suggested_eject_profile_id": exc.suggested_eject_profile_id,
            },
        ) from exc
    except BedTooHot as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "bed_hot", "bed_c": exc.bed_c, "threshold_c": exc.threshold_c},
        ) from exc
    except eject_remote.EjectDispatchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except ManualEjectError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc
