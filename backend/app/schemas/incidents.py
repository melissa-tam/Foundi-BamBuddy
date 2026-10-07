"""Wire shapes of the equipment-fault ledger read (``GET /api/v1/incidents``).

THE owner of the ledger's response types. ``schemas/fleet_metrics.py`` imports
:class:`IncidentSummary` from here (the Fleet tab's recovery summary is the same
``printer_incidents.summary``), so the dependency runs fleet → incidents and the tally
has one model.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

from backend.app.schemas.printer import PrinterMessageInfo


class IncidentSummary(BaseModel):
    """``printer_incidents.summary`` as a model — the equipment-fault ledger's tally.

    ``total`` counts EQUIPMENT FAULTS. ``declared`` counts the planned holds an operator
    opened by hand (maintenance mode), which are excluded from ``total``, ``zero_human``
    and ``by_outcome`` and appear in ``by_kind`` like any other row.
    """

    total: int
    zero_human: int
    declared: int
    by_outcome: dict[str, int]
    by_kind: dict[str, dict[str, int]]


class IncidentRow(BaseModel):
    """One equipment-fault record, with its DERIVED outcome.

    ``outcome``, ``resolution_class``, ``external``, ``slot_desc``, ``held_s``,
    ``printer_messages`` and ``recurring`` are computed at read time through the store's
    own one-origin helpers — never stored — so a change to a derivation is reflected the
    next time the row is read.
    """

    id: int
    printer_id: int
    printer_name: str | None
    job_id: str
    item_id: int | None
    kind: str
    external: bool
    code: str
    codes: str
    slot_desc: str | None
    status: str
    outcome: str
    resolution_class: str
    created_at: datetime
    escalated_at: datetime | None
    resolved_at: datetime | None
    resolve_source: str | None
    held_s: float
    #: The site's UTC offset AT ``created_at`` — a client renders the site-local wall clock
    #: from it (``tz_name`` may be an OS display name no browser can resolve).
    utc_offset_minutes: int
    printer_messages: list[PrinterMessageInfo]
    #: The row's ``(printer_id, kind, code)`` matches a printer-scope recurring line.
    recurring: bool


class RecurringWorstPrinter(BaseModel):
    """One of a fleet line's worst carriers."""

    printer_id: int
    printer_name: str
    holds: int


class RecurringLine(BaseModel):
    """One recurring-issue line (``printer_incidents.recurring_signatures``)."""

    scope: Literal["printer", "fleet"]
    printer_id: int | None
    printer_name: str | None
    kind: str
    code: str
    printer_message: PrinterMessageInfo | None
    holds: int
    days: int
    held_s: float
    last_at: datetime
    fleet_median: float
    printers_affected: int
    roster_size: int
    #: The site's UTC offset AT ``last_at``.
    utc_offset_minutes: int
    worst: list[RecurringWorstPrinter]


class IncidentsResponse(BaseModel):
    """One page of the ledger, with the window-wide tally and the recurring lines.

    ``date_from`` / ``date_to`` echo the request's inclusive SITE dates (``None`` = that
    side unbounded) in the zone named by ``tz_name``. ``total`` counts every row matching
    the filters across the window; ``summary`` is over the kind + printer filtered rows;
    ``recurring`` is over the unfiltered window.
    """

    date_from: date | None
    date_to: date | None
    tz_name: str
    total: int
    limit: int
    offset: int
    summary: IncidentSummary
    recurring: list[RecurringLine]
    items: list[IncidentRow]
