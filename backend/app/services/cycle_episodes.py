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

**Two entry points, chosen by whether the caller holds a session.**

* :func:`record_episode` — awaited, and rides the CALLER'S session inside a
  SAVEPOINT. The eject terminal (``farm_policy.on_terminal``) takes this one: it is
  async and already holds the session its own writes go through, so the measurement
  is atomic with them, opens no second connection and leaves no task running after
  the handler returns. A second connection there is not merely untidy — on SQLite it
  would sit on the busy timeout behind the terminal's own open write transaction on
  every single eject.
* :func:`note_episode` — SYNC, fire-and-forget, opens its own session. Exactly one
  caller: ``CooldownPrep.end()``, which is documented "sync, and never raises" and
  holds no session at all (its owner is a watch task, not a request).

Both share ONE validation, normalisation and row builder (:func:`_episode_row`), so
the rules about kinds, ordering and timestamp convention cannot drift between them.

**Fully guarded, both of them.** Nothing here may reach either caller: not a missing
event loop, not a database error, not a malformed argument. A measurement that cannot
be stored is a lost row; a measurement that raises is a cooldown whose fans never stop
or an eject whose plate is never resolved. The two are not close, so every failure is
one WARNING naming the printer and the kind, and nothing else — and
:func:`record_episode`'s failure is confined to its own savepoint, so the caller's
transaction is still usable and still commits.
"""

from __future__ import annotations

import logging
from collections.abc import Coroutine
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core import database as _database
from backend.app.core.tasks import spawn_background_task
from backend.app.models.farm_cycle_episode import EPISODE_KINDS, FarmCycleEpisode

logger = logging.getLogger(__name__)


async def record_episode(
    db: AsyncSession,
    printer_id: int,
    kind: str,
    *,
    started_at: datetime,
    ended_at: datetime,
    expected_s: float | None = None,
    outcome: str | None = None,
    variant: str | None = None,
) -> None:
    """Record one finished episode on the caller's session. Never raises.

    The row is inserted and flushed inside its own SAVEPOINT, so a measurement that
    cannot be written — a constraint, a corrupt value — rolls back only itself and the
    caller's transaction carries on. It deliberately does NOT commit: the caller owns
    the transaction, and a measurement is never a reason to publish somebody else's
    half-finished work.

    The flush is restricted to this row (``flush([row])``). A blanket flush inside the
    savepoint would drag the caller's own pending changes in with it, and a failure
    would then roll THOSE back too — turning a lost measurement into lost farm state.
    """
    try:
        row = _episode_row(
            printer_id,
            kind,
            started_at=started_at,
            ended_at=ended_at,
            expected_s=expected_s,
            outcome=outcome,
            variant=variant,
        )
        if row is None:
            return
        async with db.begin_nested():
            db.add(row)
            await db.flush([row])
    except Exception:  # noqa: BLE001 — a measurement never disturbs the work it measured
        logger.warning("cycle episode: printer %s %s not recorded", printer_id, kind, exc_info=True)


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
        row = _episode_row(
            printer_id,
            kind,
            started_at=started_at,
            ended_at=ended_at,
            expected_s=expected_s,
            outcome=outcome,
            variant=variant,
        )
        if row is None:
            return
        coro = _store(row, printer_id=printer_id, kind=kind)
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


async def _store(row: FarmCycleEpisode, *, printer_id: int, kind: str) -> None:
    """Insert ``row`` on a session of our own. The awaitable seam the spawned task runs.

    Only :func:`note_episode` reaches here, and only because its caller has no session
    to lend. The session comes from the module attribute rather than a bound import —
    the accommodation ``library_integrity`` takes — so reading
    ``_database.async_session`` at call time is what lets a test point it at its own
    engine.
    """
    try:
        async with _database.async_session() as db:
            db.add(row)
            await db.commit()
    except Exception:  # noqa: BLE001 — see the module docstring: a lost row, never a raised one
        logger.warning("cycle episode: printer %s %s not stored", printer_id, kind, exc_info=True)


def _episode_row(
    printer_id: int,
    kind: str,
    *,
    started_at: datetime,
    ended_at: datetime,
    expected_s: float | None,
    outcome: str | None,
    variant: str | None,
) -> FarmCycleEpisode | None:
    """Validate, normalise and build the row — or ``None``, having said why.

    THE one place the ledger's rules live, so the two entry points cannot come to
    disagree about what is recordable. A ``kind`` outside :data:`EPISODE_KINDS` and an
    episode that ends before it starts are refused: the ledger's whole value is that
    every row in it is a real duration, so a row nobody can interpret is worse than a
    missing one.

    The row it returns is transient — it belongs to no session yet, which is what lets
    the caller decide whether it goes into its own transaction or into one of ours.
    """
    if kind not in EPISODE_KINDS:
        logger.warning("cycle episode: printer %s — unknown kind %r, not recorded", printer_id, kind)
        return None
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
        return None
    return FarmCycleEpisode(
        printer_id=printer_id,
        kind=kind,
        started_at=start,
        ended_at=end,
        expected_s=expected_s,
        outcome=outcome,
        variant=variant,
    )


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
