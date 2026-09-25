"""The sampler behind :class:`PrinterObservationSpan` — THE writer of fleet history.

Everything the farm knows about a printer's condition is current-value-only: the MQTT
session, the live status push, two in-memory flag sets and the plate authority's
record all answer "now" and remember nothing. This loop reads those answers every
30 s and run-length-compresses them into spans, so that "how many printers were down
last Tuesday, and why" has an answer at all.

**It records, it does not classify.** :class:`Observation` is seven orthogonal facts
read off one printer at one instant, and it is also the compression key: while the
tuple is unchanged the loop only bumps the open span's ``last_observed_at``. What
"down" means is decided at READ time by the classifier, over these rows — which is
why nothing here ranks the columns against each other, and why the one derived value
this module does compute (:data:`plate_phase`) is a single axis with one authority
behind it rather than a verdict about the printer.

**Silence is recorded as silence.** A tick that cannot read a printer honestly writes
nothing and bumps nothing (see :func:`gather_observation`), and an open span whose
last sample has gone stale is closed AT that sample rather than extended to now. The
uncovered stretch left behind is a hole in the record, which is the truth: a
controller restart, a sleeping host and a stalled loop all produce one, and there is
deliberately no startup pass trying to paper over it — the ONE stale rule covers all
three, on whichever tick the loop next runs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core import database as _database
from backend.app.models.printer import Printer
from backend.app.models.printer_observation_span import (
    PLATE_PHASE_CLEAR,
    PLATE_PHASE_COOLING,
    PLATE_PHASE_EJECTING,
    PLATE_PHASE_HELD,
    PrinterObservationSpan,
)
from backend.app.services import usb_storage
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.eject import remote as eject_remote
from backend.app.services.eject.monitor import eject_cooldown_monitor
from backend.app.services.plate_occupancy import plate_occupancy
from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)

# The sampling cadence. Fast enough that a minutes-long condition is dated to the
# minute, slow enough that the whole fleet costs one SELECT and a handful of one-column
# UPDATEs per tick. Short episodes (an eject, a cooldown) are NOT this instrument's job
# — they measure themselves into ``farm_cycle_episode``.
_SAMPLE_INTERVAL_S = 30

# How old an open span's last sample may be before the loop refuses to extend it. Six
# missed ticks: long enough that a slow tick or a brief loop stall is still one
# continuous span, short enough that a restart gap is visible as a gap rather than
# being absorbed into whatever the printer happened to read before it.
#
# PUBLIC because the read side applies the identical rule to an OPEN span: fresh
# (``now - last_observed_at <= STALE_AFTER_S``) means the span covers up to now, stale
# means its coverage ended at ``last_observed_at`` and the rest is a hole. One
# definition, so the writer and the timeline can never disagree about what a still-open
# row is evidence of.
STALE_AFTER_S = 180

# How long after process start a printer that has never connected IN THIS PROCESS is
# allowed to be silent without being recorded as offline. Every session is still
# dialling for the first minute of a restart, and recording "offline" there would
# manufacture downtime out of the controller's own boot.
_STARTUP_GRACE_S = 90.0

# The printer's own state word is stored in a String(16) column. The vocabulary
# (RUNNING / PAUSE / FINISH / IDLE / FAILED / PREPARE / SLICING …) fits with room to
# spare; the bound exists so an unknown longer word lands as a truncated reading
# instead of a database error that would cost the whole tick.
_GCODE_STATE_MAX = 16


@dataclass(frozen=True, slots=True)
class Observation:
    """What one printer read like at one instant — and the run-length key.

    Frozen and compared by value on purpose: "the same span" IS "an equal
    Observation", so the compression rule has no second definition to drift from.
    Field for field it mirrors the observed half of :class:`PrinterObservationSpan`.
    """

    is_active: bool
    connected: bool
    gcode_state: str | None
    plate_phase: str
    quarantined: bool
    usb_present: bool | None
    model_mismatch: bool


class GatherObservation(Protocol):
    """The reading half, as :meth:`FleetActivityRecorder.sample_once` injects it."""

    def __call__(self, printer_id: int, is_active: bool, *, uptime_s: float) -> Observation | None: ...


def gather_observation(printer_id: int, is_active: bool, *, uptime_s: float) -> Observation | None:
    """Read one printer. Sync, DB-free, and ``None`` when it cannot be read honestly.

    ``None`` means *write nothing, bump nothing* — not "offline". There are exactly
    two such cases, and both are the controller admitting it does not know yet:

      * the printer has never connected in this process and the process is younger
        than :data:`_STARTUP_GRACE_S` (the session is still dialling);
      * it is connected but names no state, or names ``unknown`` — its first full
        report has not landed, and the transport's placeholder is not a reading.

    A DEACTIVATED printer is never ``None``: "out of fleet" is a known fact from the
    first tick, it has no session BY DESIGN, and waiting for one would leave the
    deactivated half of the fleet unrecorded forever.

    An accessor that raises costs this printer its tick and nothing more — one
    WARNING, ``None``, and the loop carries on to the next machine.
    """
    try:
        status = printer_manager.get_status(printer_id)
        # A deactivated printer is forced to ``connected=False`` rather than asked:
        # the operator withdrew it, so whatever a lingering session says about it is
        # not a fact about the fleet.
        connected = is_active and status is not None and bool(status.connected)
        gcode_state: str | None = None
        if connected:
            gcode_state = _normalise_state(status.state if status is not None else None)
            if gcode_state is None:
                return None
        elif is_active and _never_connected(status) and uptime_s < _STARTUP_GRACE_S:
            return None
        return Observation(
            is_active=is_active,
            connected=connected,
            # Everything a dead session cannot vouch for is normalised away: a state
            # held over from before the drop would flap the tuple on reconnect and
            # read, afterwards, as an observation that was never made. ``usb_present``
            # already answers None for an unreachable printer, and the remaining three
            # are SERVER-side facts that stay real while the wire is down.
            gcode_state=gcode_state,
            plate_phase=_plate_phase(printer_id, status if connected else None),
            quarantined=printer_manager.is_quarantined(printer_id),
            usb_present=usb_storage.usb_present(printer_id),
            model_mismatch=printer_manager.is_model_mismatch(printer_id),
        )
    except Exception:  # noqa: BLE001 — one unreadable printer never costs the tick
        logger.warning("fleet activity: printer %s could not be read this tick", printer_id, exc_info=True)
        return None


def _never_connected(status: PrinterState | None) -> bool:
    """Has this printer had NO MQTT session at all since the process started?

    Two shapes, both meaning the same thing: no state object (no client was ever
    built for it) and a state whose ``connection_epoch`` is still 0 (a client exists
    and has never completed a session — the transport itself owns that counter and
    increments it once per successful connect).
    """
    return status is None or status.connection_epoch == 0


def _normalise_state(raw: str | None) -> str | None:
    """The printer's own word, upper-cased — or ``None`` when it named nothing.

    ``unknown`` is the transport's initial placeholder, not a report, so it is treated
    exactly like an empty string: there is no honest reading yet.
    """
    if not raw:
        return None
    text = raw.strip().upper()
    if not text or text == "UNKNOWN":
        return None
    return text[:_GCODE_STATE_MAX]


def _plate_phase(printer_id: int, status: PrinterState | None) -> str:
    """The plate axis, first match wins — ONE reading, from the owners of each fact.

    The order is physical, not a ranking of severity: a sweep in flight is happening
    whatever else is true, a COOLING watch is a bed on its way down (with or without an
    eject line to quote — shop air can be unknown), and only then does an occupied plate
    mean "waiting on a person".
    A DEFERRED watch (cooled, fans retired, the eject withheld under a service hold)
    deliberately falls through to ``held``: the thermal work is over, and what the
    plate is now waiting for is a human lifting the hold — which is what the read-time
    classifier reads together with the open incident to call it planned.

    ``status`` is passed only when the session is live, so a stale ``subtask_name``
    from a dropped connection cannot report a sweep that ended long ago.
    """
    view = plate_occupancy.current_view(printer_id)
    if view.eject_present or (status is not None and eject_remote.is_eject_job_name(status.subtask_name)):
        return PLATE_PHASE_EJECTING
    if eject_cooldown_monitor.cooling_watch(printer_id) is not None and not eject_cooldown_monitor.deferred(printer_id):
        return PLATE_PHASE_COOLING
    if view.plate_occupied:
        return PLATE_PHASE_HELD
    return PLATE_PHASE_CLEAR


class FleetActivityRecorder:
    """The 30 s loop, and the one tick it is made of."""

    def __init__(self) -> None:
        self._scheduler_task: asyncio.Task[None] | None = None
        # Monotonic process start, stamped at start() rather than at import: the grace
        # window is about how long the CONTROLLER has been up, and a module imported
        # during a slow boot would date it minutes early.
        self._started_at: float | None = None

    async def start(self) -> None:
        if self._scheduler_task is not None:
            return
        self._started_at = time.monotonic()
        logger.info("Starting fleet activity recorder")
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    def stop(self) -> None:
        if self._scheduler_task:
            self._scheduler_task.cancel()
            self._scheduler_task = None
            self._started_at = None
            logger.info("Stopped fleet activity recorder")

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                async with _database.async_session() as db:
                    await self.sample_once(db, now=_utcnow(), uptime_s=self.uptime_s())
                await asyncio.sleep(_SAMPLE_INTERVAL_S)
            except asyncio.CancelledError:
                break
            except Exception as e:  # pragma: no cover - defensive
                # A failed tick is a gap, and the stale rule already knows how to read
                # one. Keep the cadence rather than backing off: the next tick is the
                # repair, and a longer sleep would only widen the hole.
                logger.error("Error in fleet activity recorder: %s", e)
                await asyncio.sleep(_SAMPLE_INTERVAL_S)

    def uptime_s(self) -> float:
        """How long this process has been recording, in seconds. 0.0 before ``start()``.

        PUBLIC because the live-status endpoint reads a printer through the same
        ``gather_observation(..., uptime_s=fleet_activity_recorder.uptime_s())``: "now"
        and history then apply one startup grace, so a printer that is still dialling
        one minute after a restart reads as not-yet-known on the tile exactly as it
        does in the log, instead of being offline on one surface and absent on the
        other.
        """
        if self._started_at is None:
            return 0.0
        return time.monotonic() - self._started_at

    async def sample_once(
        self,
        db: AsyncSession,
        *,
        now: datetime,
        uptime_s: float,
        gather: GatherObservation = gather_observation,
    ) -> None:
        """Sample every printer once and write the tick. ``now`` is naive UTC, whole seconds.

        Two selects, then one write transaction: the roster as bare columns and the
        open spans as rows to mutate. Each printer is applied inside its OWN SAVEPOINT,
        so a corrupt reading or a unique-index collision on one machine costs that
        machine its tick and leaves the other eleven written.
        """
        roster = (await db.execute(select(Printer.id, Printer.is_active))).all()
        open_spans = (
            (await db.execute(select(PrinterObservationSpan).where(PrinterObservationSpan.ended_at.is_(None))))
            .scalars()
            .all()
        )
        open_by_printer = {span.printer_id: span for span in open_spans}
        # Captured as plain ints BEFORE any write. A savepoint rollback EXPIRES every
        # instance it dirtied, and reading an expired span's attribute afterwards is an
        # implicit lazy refresh — MissingGreenlet under an AsyncSession, raised from
        # wherever the read happens. The orphan sweep below runs after the writes, so a
        # single ``span.printer_id`` there would turn ONE printer's failed savepoint
        # into a tick that dies before its commit and loses the whole fleet's sample.
        span_ids = {printer_id: span.id for printer_id, span in open_by_printer.items()}

        for printer_id, is_active in roster:
            try:
                observation = gather(printer_id, bool(is_active), uptime_s=uptime_s)
                if observation is None:
                    # No honest reading. The open span simply ages; if the silence
                    # lasts, the stale rule closes it at its own last sample on a
                    # later tick, which is the honest record of a hole.
                    continue
                async with db.begin_nested():
                    await _apply(db, open_by_printer.get(printer_id), printer_id, observation, now)
            except Exception:  # noqa: BLE001 — one printer's failure is not the tick's
                logger.warning("fleet activity: printer %s not recorded this tick", printer_id, exc_info=True)

        roster_ids = {printer_id for printer_id, _ in roster}
        for printer_id, span in open_by_printer.items():
            if printer_id in roster_ids:
                continue
            # The printer row is gone. History outlives the equipment, so the span is
            # closed by the same fresh/stale rule and kept. This span cannot have been
            # expired above — its printer was not in the roster, so no savepoint
            # touched it — and every attribute read on it is inside the guard anyway.
            try:
                async with db.begin_nested():
                    _close(span, now)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "fleet activity: span %s for deleted printer %s not closed",
                    span_ids[printer_id],
                    printer_id,
                    exc_info=True,
                )

        await db.commit()


async def _apply(
    db: AsyncSession,
    span: PrinterObservationSpan | None,
    printer_id: int,
    observation: Observation,
    now: datetime,
) -> None:
    """Fold one reading into this printer's open span. THE run-length rule.

    ``eff_now`` is ``max(now, last_observed_at)`` throughout: a clock stepped backwards
    (an NTP correction, a host waking up) must never produce a span that ends before it
    starts, nor a ``last_observed_at`` that moves backwards and makes a later tick read
    the span as fresher than it is.
    """
    if span is None:
        db.add(_open_span(printer_id, observation, now))
        return

    if _is_stale(span, now):
        # The gap is the record. Close at the last sample that actually saw this
        # tuple — never at ``now``, which would claim the printer read that way
        # through a stretch nothing observed — and start the new span at ``now``.
        span.ended_at = span.last_observed_at
        await db.flush()
        db.add(_open_span(printer_id, observation, now))
        return

    eff_now = max(now, span.last_observed_at)
    if _observed(span) == observation:
        span.last_observed_at = eff_now
        return

    # The tuple changed: the two spans MEET at one instant, so the timeline has
    # neither a gap nor an overlap. The close is flushed before the new row is added
    # because the partial unique index allows exactly one open span per printer.
    span.ended_at = eff_now
    span.last_observed_at = eff_now
    await db.flush()
    db.add(_open_span(printer_id, observation, eff_now))


def _close(span: PrinterObservationSpan, now: datetime) -> None:
    """End an open span with no successor — the deleted-printer case."""
    if _is_stale(span, now):
        span.ended_at = span.last_observed_at
        return
    eff_now = max(now, span.last_observed_at)
    span.ended_at = eff_now
    span.last_observed_at = eff_now


def _is_stale(span: PrinterObservationSpan, now: datetime) -> bool:
    return (now - span.last_observed_at).total_seconds() > STALE_AFTER_S


def _observed(span: PrinterObservationSpan) -> Observation:
    """The span's stored tuple, as the value the next reading is compared against."""
    return Observation(
        is_active=span.is_active,
        connected=span.connected,
        gcode_state=span.gcode_state,
        plate_phase=span.plate_phase,
        quarantined=span.quarantined,
        usb_present=span.usb_present,
        model_mismatch=span.model_mismatch,
    )


def _open_span(printer_id: int, observation: Observation, started_at: datetime) -> PrinterObservationSpan:
    return PrinterObservationSpan(
        printer_id=printer_id,
        started_at=started_at,
        last_observed_at=started_at,
        ended_at=None,
        is_active=observation.is_active,
        connected=observation.connected,
        gcode_state=observation.gcode_state,
        plate_phase=observation.plate_phase,
        quarantined=observation.quarantined,
        usb_present=observation.usb_present,
        model_mismatch=observation.model_mismatch,
    )


def _utcnow() -> datetime:
    """The loop's clock: naive UTC at whole seconds, the table's own convention."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


# Module-level singleton, mirroring the other service singletons.
fleet_activity_recorder = FleetActivityRecorder()
