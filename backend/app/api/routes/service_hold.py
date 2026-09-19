"""Maintenance mode — enter and leave a printer's service hold.

Two verbs over ``services.service_hold``, which owns every decision: this module is a
404 check, a permission and a response shape. The UI label is "Maintenance mode"; the
code says ``service_hold`` throughout, because the fork already has a preventive
``maintenance`` subsystem (``models/maintenance.py``, the "maintenance due" chip on the
same card) and the two must never be greppable as one thing.

Its own module rather than more of the 159 KB ``printers.py``, per the fork's
large-route-file convention.

Permission ``PRINTERS_CONTROL``: entering the hold STOPS an in-flight sweep and revokes
a dispatch lease, which is the stop/pause/motion class — ``PRINTERS_UPDATE`` is the
*edit the printer record* permission and deactivation (the other, unrelated flag) keeps
it. (It does NOT stop a running print: no mode verb ends a print, only Stop does.)
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.schemas.printer import ServiceHoldEnterResponse, ServiceHoldExitResponse
from backend.app.services import service_hold

router = APIRouter(prefix="/printers", tags=["printers"])


def _actor(user: User | None) -> str:
    """Who is on the record for this hold.

    ``None`` is the auth-disabled / API-key principal: the log line says ``local``
    rather than inventing a name, which is what every other single-trust surface does.
    """
    return getattr(user, "username", None) or "local"


async def _require_printer(db: AsyncSession, printer_id: int) -> Printer:
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if printer is None:
        raise HTTPException(404, "Printer not found")
    return printer


@router.post("/{printer_id}/service-hold", response_model=ServiceHoldEnterResponse)
async def enter_service_hold(
    printer_id: int,
    user: User | None = RequirePermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
    db: AsyncSession = Depends(get_db),
):
    """Put a printer in maintenance mode: record the hold, then quiesce its automation.

    Idempotent — a second call on a held printer answers ``already_held: true`` and
    quiesces again (that is usually WHY it is being clicked). A DEACTIVATED printer is
    accepted too: there is nothing to quiesce with no session, and the hold is still the
    thing that stops the kick-driven scheduler dispatching within ~1 s of re-activation.
    """
    await _require_printer(db, printer_id)
    verdict = await service_hold.enter(db, printer_id, actor=_actor(user))
    return ServiceHoldEnterResponse(**asdict(verdict))


@router.delete("/{printer_id}/service-hold", response_model=ServiceHoldExitResponse)
async def exit_service_hold(
    printer_id: int,
    user: User | None = RequirePermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
    db: AsyncSession = Depends(get_db),
):
    """Take a printer out of maintenance mode.

    ``released: false`` for a printer that was not held — the verb is idempotent, and a
    404/409 there would make the UI's exit button fail on the state it is trying to reach.
    """
    await _require_printer(db, printer_id)
    released = await service_hold.exit(db, printer_id, actor=_actor(user))
    return ServiceHoldExitResponse(released=released)
