"""Power-stagger policy — the single owner of "how many printers may START now"
(latency Phase E).

Beds draw their dominant current during the ~90 s heat-up ramp right after a
dispatch. To cap simultaneous electrical load the scheduler admits at most
``stagger_group_size`` *starts* per rolling ``stagger_interval_minutes`` window.

Phase E replaces the pure time-window count (which delivered a sluggish
pair-to-pair cadence of ~5.6 min even though beds reach target in 2-3 min) with a
**bed-temperature dynamic release**: a started printer occupies a stagger slot
only while its bed is still RAMPING; the slot frees the moment the bed reaches
``target − stagger_release_epsilon_c``. The time window remains the hard
ceiling / fail-safe (an item outside the window never occupies), so with
``stagger_dynamic_release`` OFF the behaviour is byte-for-byte the old count.

This module also owns the **in-flight set** — dispatches that have been PLANNED
this tick but whose durable ``started_at`` row is not yet written. It is the
single source of truth that stops a kick landing mid-gather from over-admitting a
heater: ``budget`` = ``group_size − (len(in_flight) + occupied_recent_starts)``.

Restart-safe by design: both the in-flight set and the ramp-watch are in-memory
only. After a restart ``budget`` re-derives from durable ``started_at`` rows plus
live printer status, so a thundering herd cannot be unleashed.

LEAF-ward imports only: ``printer_manager`` + ``dispatch_kick`` (both lower
layers) and the ``Settings``/``PrintQueueItem`` models. NEVER imports
``print_scheduler`` — the scheduler imports this module, not the reverse.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.settings import Settings
from backend.app.services.dispatch_kick import dispatch_kick
from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)

# Defaults mirror the AppSettings schema so the ramp-watch behaves sanely before
# the first ``budget`` call refreshes the cache from the DB.
_DEFAULT_GROUP_SIZE = 2
_DEFAULT_INTERVAL_MINUTES = 3
_DEFAULT_DYNAMIC = True
_DEFAULT_EPSILON_C = 2.0
_DEFAULT_GRACE_SECONDS = 120


@dataclass
class _RampWatch:
    """One armed bed-ramp watch: fires a ``bed_at_target`` kick once, then drops."""

    item_id: int
    armed_at: float  # time.monotonic()


class StaggerPolicy:
    """Module singleton owning stagger admission, the in-flight set + ramp-watch."""

    def __init__(self) -> None:
        # item_id -> printer_id for dispatches PLANNED but not yet settled. These
        # occupy the budget even though no durable started_at row exists yet.
        self._in_flight: dict[int, int] = {}
        # printer_id -> ramp watch armed at plan time; on_status_push fires the
        # bed-at-target kick and drops the entry.
        self._ramp_watch: dict[int, _RampWatch] = {}
        # Cache of the tunables, refreshed on every budget() call so the sync
        # on_status_push / note_dispatch_planned paths (which have no DB handle)
        # can read them without a query.
        self._dynamic: bool = _DEFAULT_DYNAMIC
        self._epsilon_c: float = _DEFAULT_EPSILON_C
        self._grace_seconds: int = _DEFAULT_GRACE_SECONDS
        self._window_seconds: float = _DEFAULT_INTERVAL_MINUTES * 60

    # ---- budget ---------------------------------------------------------

    async def budget(self, db: AsyncSession) -> int:
        """Remaining starts allowed right now (>= 0).

        ``group_size − (len(in_flight) + occupied_recent_starts)``, floored at 0.
        ``occupied_recent_starts`` counts started-within-window items whose
        printer is still ramping (dynamic release) or, with dynamic release OFF,
        simply every started-within-window item (the exact old behaviour). Items
        currently in the in-flight set are excluded from the window count so a
        settling dispatch is never double-charged during the started_at handoff.
        """
        group_size = await self._get_int(db, "stagger_group_size", _DEFAULT_GROUP_SIZE)
        interval_minutes = await self._get_int(db, "stagger_interval_minutes", _DEFAULT_INTERVAL_MINUTES)
        dynamic = await self._get_bool(db, "stagger_dynamic_release", _DEFAULT_DYNAMIC)
        epsilon = await self._get_float(db, "stagger_release_epsilon_c", _DEFAULT_EPSILON_C)
        grace = await self._get_int(db, "stagger_heatup_grace_seconds", _DEFAULT_GRACE_SECONDS)

        # Refresh the cache used by the sync ramp-watch paths.
        self._dynamic = dynamic
        self._epsilon_c = epsilon
        self._grace_seconds = grace
        self._window_seconds = max(1, interval_minutes) * 60

        if group_size <= 0 or interval_minutes <= 0:
            # Defensive: schema clamps these to >=1, but a hand-edited row could
            # slip through — treat non-positive config as "staggering off".
            return group_size if group_size > 0 else 1_000_000

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(minutes=interval_minutes)
        rows = (
            await db.execute(
                select(PrintQueueItem.id, PrintQueueItem.printer_id, PrintQueueItem.started_at).where(
                    PrintQueueItem.started_at >= window_start
                )
            )
        ).all()

        occupied = 0
        for item_id, printer_id, started_at in rows:
            if item_id in self._in_flight:
                # Counted via the in-flight set — don't double-charge across the
                # started_at handoff.
                continue
            if self._occupies(printer_id, started_at, now, dynamic, epsilon, grace):
                occupied += 1

        return max(0, group_size - (len(self._in_flight) + occupied))

    def _occupies(
        self,
        printer_id: int | None,
        started_at: datetime | None,
        now: datetime,
        dynamic: bool,
        epsilon: float,
        grace: int,
    ) -> bool:
        """Does this recent-start still hold a stagger slot?

        Dynamic release OFF short-circuits to True (any in-window start occupies —
        the exact legacy behaviour, SAME code path). Otherwise judge from live
        bed temperature, failing SAFE (occupied) whenever status is unavailable.
        """
        if not dynamic:
            return True

        status = printer_manager.get_status(printer_id) if printer_id is not None else None
        if status is None or not getattr(status, "connected", False):
            # No live evidence the ramp finished — fail safe.
            return True

        temps = getattr(status, "temperatures", None) or {}
        bed_target = temps.get("bed_target")
        bed = temps.get("bed")

        if started_at is not None and started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        elapsed = (now - started_at).total_seconds() if started_at is not None else 0.0

        if bed_target is None or bed_target <= 0:
            # Firmware prep before heating begins reports no bed target yet; hold
            # the slot until the grace window elapses (covers the ~50 s pre-heat).
            return elapsed < grace

        if bed is None:
            # Target set but no bed reading — can't confirm the ramp finished.
            return True
        return bed < bed_target - epsilon

    # ---- in-flight set --------------------------------------------------

    def note_dispatch_planned(self, printer_id: int, item_id: int) -> None:
        """Enter the in-flight set (called at plan time) and arm the ramp-watch.

        The set makes ``budget`` correct across kicks before the durable
        ``started_at`` row exists; the ramp-watch fires the bed-at-target kick.
        """
        self._in_flight[item_id] = printer_id
        self._ramp_watch[printer_id] = _RampWatch(item_id=item_id, armed_at=time.monotonic())

    def note_dispatch_settled(self, item_id: int) -> None:
        """Leave the in-flight set (called on EVERY dispatch outcome).

        Success: the item's ``started_at``/status flip lets the durable window
        record take over. Failure/skip/hold: the slot frees immediately. The
        ramp-watch is intentionally NOT cleared here — it survives to fire when
        the bed (which ramps during printing) reaches target.
        """
        self._in_flight.pop(item_id, None)

    # ---- ramp-watch -----------------------------------------------------

    def on_status_push(self, printer_id: int, status) -> None:
        """Fire a ``bed_at_target`` kick once when a watched printer's bed crosses
        ``target − epsilon``. Cheap: returns immediately for un-watched printers.
        """
        entry = self._ramp_watch.get(printer_id)
        if entry is None:
            return
        now = time.monotonic()
        # Lazy prune: the watch expires at the window ceiling.
        if now - entry.armed_at > self._window_seconds:
            self._ramp_watch.pop(printer_id, None)
            return
        if not self._dynamic:
            # With dynamic release off a bed-at-target kick frees no slot.
            return
        temps = getattr(status, "temperatures", None) or {}
        bed_target = temps.get("bed_target")
        bed = temps.get("bed")
        if bed_target is None or bed_target <= 0 or bed is None:
            return
        if bed >= bed_target - self._epsilon_c:
            # Fire once, then drop — a second crossing without re-arm won't re-fire.
            self._ramp_watch.pop(printer_id, None)
            dispatch_kick.kick("bed_at_target", printer_id)

    # ---- test / introspection helpers -----------------------------------

    def reset(self) -> None:
        """Clear all in-memory state + restore cached tunables (test isolation)."""
        self._in_flight.clear()
        self._ramp_watch.clear()
        self._dynamic = _DEFAULT_DYNAMIC
        self._epsilon_c = _DEFAULT_EPSILON_C
        self._grace_seconds = _DEFAULT_GRACE_SECONDS
        self._window_seconds = _DEFAULT_INTERVAL_MINUTES * 60

    @property
    def in_flight_count(self) -> int:
        return len(self._in_flight)

    # ---- settings reads (leaf; can't use the scheduler helpers) ----------

    @staticmethod
    async def _get_int(db: AsyncSession, key: str, default: int) -> int:
        setting = (await db.execute(select(Settings).where(Settings.key == key))).scalar_one_or_none()
        if setting and setting.value is not None:
            try:
                return int(setting.value)
            except (TypeError, ValueError):
                return default
        return default

    @staticmethod
    async def _get_float(db: AsyncSession, key: str, default: float) -> float:
        setting = (await db.execute(select(Settings).where(Settings.key == key))).scalar_one_or_none()
        if setting and setting.value is not None:
            try:
                return float(setting.value)
            except (TypeError, ValueError):
                return default
        return default

    @staticmethod
    async def _get_bool(db: AsyncSession, key: str, default: bool) -> bool:
        setting = (await db.execute(select(Settings).where(Settings.key == key))).scalar_one_or_none()
        if setting and setting.value is not None:
            return str(setting.value).strip().lower() == "true"
        return default


# Module-level singleton — the one stagger policy shared across the process.
stagger_policy = StaggerPolicy()
