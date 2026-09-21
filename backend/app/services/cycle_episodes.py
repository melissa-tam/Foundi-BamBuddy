"""The writer behind :class:`FarmCycleEpisode` — one call, from the owner that measured.

The cooldown wait and the eject sweep each already time themselves, because each has
to in order to decide that it is over. This module is the one place that measurement
is STORED instead of being formatted into a log line and forgotten; nothing else may
construct an episode row.

**A leaf, deliberately.** ``eject.monitor`` imports ``eject.cooldown_prep``, and the
fleet observation recorder imports ``eject.monitor`` — so a writer living beside the
recorder would close an import cycle the moment ``cooldown_prep`` called it. This
module therefore imports nothing from ``services``: core, models and the standard
library only, which is what lets both measuring owners call it directly.

**Fire-and-forget, and fully guarded.** :func:`note_episode` is SYNC and called from
inside ``CooldownPrep.end()`` — documented as "sync, and never raises" — and from the
async eject terminal. Nothing it does may reach either caller: not a missing event
loop, not a database error, not a malformed argument. A measurement that cannot be
stored is a lost row; a measurement that raises is a cooldown whose fans never stop or
an eject whose plate is never resolved. The two are not close, so every failure here
is one WARNING naming the printer and the kind, and nothing else.
"""

from __future__ import annotations

import logging
from collections.abc import Coroutine
from datetime import datetime, timezone

from backend.app.core import database as _database
from backend.app.core.tasks import spawn_background_task
from backend.app.models.farm_cycle_episode import EPISODE_KINDS, FarmCycleEpisode

logger = logging.getLogger(__name__)


def note_episode(
    printer_id: int,
    kind: str,
    *,
    started_at: datetime,
    ended_at: datetime,
    expected_s: float | None = None,
    outcome: str | None = None,
    variant: str | None = None,
) -> None:
    """Record one finished episode. Never raises, never blocks its caller.

    ``started_at`` / ``ended_at`` may be aware or naive-UTC; both are stored as naive
    UTC at second precision, the table's own convention. A ``kind`` outside
    :data:`EPISODE_KINDS` and an episode that ends before it starts are DROPPED with a
    WARNING rather than written: the ledger's value is that every row in it is a real
    duration, so a row nobody can interpret is worse than a missing one.
    """
    coro: Coroutine[object, object, None] | None = None
    try:
        if kind not in EPISODE_KINDS:
            logger.warning("cycle episode: printer %s — unknown kind %r, not recorded", printer_id, kind)
            return
        start = _naive_utc(started_at)
        end = _naive_utc(ended_at)
        if end < start:
            logger.warning(
                "cycle episode: printer %s %s ends (%s) before it starts (%s), not recorded",
                printer_id,
                kind,
                end,
                start,
            )
            return
        coro = _store(
            printer_id,
            kind,
            started_at=start,
            ended_at=end,
            expected_s=expected_s,
            outcome=outcome,
            variant=variant,
        )
        # Named so a leaked task is traceable to this spawn site rather than to the
        # generic helper. The helper keeps the strong reference; we keep none — the
        # write is the whole point and there is nothing here to cancel it with.
        spawn_background_task(coro, name=f"cycle-episode-{kind}-printer-{printer_id}")
    except Exception:  # noqa: BLE001 — a measurement never disturbs the process it measured
        if coro is not None:
            # No running loop (a sync caller outside the app, a test): the coroutine
            # was built and never scheduled, and an un-awaited coroutine would surface
            # as a RuntimeWarning from the garbage collector minutes later, in some
            # unrelated task's traceback.
            coro.close()
        logger.warning("cycle episode: printer %s %s not recorded", printer_id, kind, exc_info=True)


async def _store(
    printer_id: int,
    kind: str,
    *,
    started_at: datetime,
    ended_at: datetime,
    expected_s: float | None,
    outcome: str | None,
    variant: str | None,
) -> None:
    """Insert the row on its own session. The awaitable seam the spawned task runs.

    Its own session, via the module attribute rather than a bound import, for the
    reason ``library_integrity`` takes one: the callers have no session to lend (the
    cooldown's ``end()`` is sync, and the eject terminal's session belongs to a
    transaction whose commit must not carry a measurement's failure), and reading
    ``_database.async_session`` at call time is what lets a test point it at its own
    engine.
    """
    try:
        async with _database.async_session() as db:
            db.add(
                FarmCycleEpisode(
                    printer_id=printer_id,
                    kind=kind,
                    started_at=started_at,
                    ended_at=ended_at,
                    expected_s=expected_s,
                    outcome=outcome,
                    variant=variant,
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — see the module docstring: a lost row, never a raised one
        logger.warning("cycle episode: printer %s %s not stored", printer_id, kind, exc_info=True)


def _naive_utc(value: datetime) -> datetime:
    """The table's timestamp convention: naive UTC, whole seconds.

    An aware input is converted rather than stripped — a caller that measured in local
    time would otherwise store a figure hours away from every other row — and the
    sub-second part is dropped because the column is read as a duration in seconds and
    a stored microsecond would only make two equal episodes compare unequal.
    """
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)
