"""Notification dedup — one alert per incident, across flaps AND across restarts.

Two layers, one owner:

**In-memory (per process).** :func:`new_codes` keeps a per-(printer, code)
last-seen ledger. A code is "new" (worth notifying) only on its FIRST appearance
for a printer, or after it has been ABSENT at least
:data:`_HMS_RENOTIFY_ABSENT_SECONDS`. Production incident 2026-07-18: HMS code
``0700_0002`` flapped in and out of a printer's ``hms`` list ~80 times in 2.5 h
and each RETURN re-notified, because main's old dedup REPLACED the whole
per-printer notified set on every push and only cleared on a fully-empty list.
:func:`allow` is the same idea generalised to any ``(scope, key)`` for callers
that fire on a tick rather than on an edge.

**Durable (across processes).** The in-memory layer alone re-blasts every
STANDING code at each deploy — 2026-07-20 00:45: six printers re-announced their
standing codes within 7 s, and several of those codes (a failed read on a tagless
slot) can never clear, so operators got the same alerts at every single deploy.
:func:`seed_standing` closes that: on the FIRST status push per printer per
process it reads the durable
:class:`~backend.app.models.notification_ledger.NotificationLedger` (one read per
printer per process) and pre-marks as *already seen* every live code we have
provably alerted on before. A live code with NO durable row is left alone — a
fault that arose while the server was down MUST still notify.

The durable table is written ONLY where the operator was actually informed
(:func:`record_sent`), never speculatively: a row means "we told the operator
about this", so absence always fails towards notifying. "Informed" includes the
recovery-owned case: when spool_recovery owns an incident the raw per-code HMS
alert is SUPPRESSED (recovery's own lifecycle notifications are the operator
signal), but the code is still recorded at suppression time — otherwise a standing
owned code, having never stamped a row, would re-blast at the next deploy via
:func:`seed_standing`.

Module-level in-memory state is process-lifetime, matching the fork's other
event-edge bookkeeping (``ams_presence._last_presence``,
``spool_respool._last_tray_now``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.notification_ledger import NotificationLedger

logger = logging.getLogger(__name__)

# Scope name for the printer-HMS key space in the durable ledger.
HMS_SCOPE = "hms"

# Durable rows untouched this long are dropped at startup: a key absent that long
# re-notifies as new anyway (the in-memory window is 10 minutes), so pruning is
# behaviour-preserving and bounds the table.
LEDGER_PRUNE_DAYS = 30

# printer_id -> {code -> last-seen wall-clock timestamp}. Wall clock (time.time())
# to match the caller in main.on_printer_status_change; the value is whatever
# monotonic-or-wall ``now`` the caller passes, used only for elapsed math.
_last_seen: dict[int, dict[str, float]] = {}

# Printers whose durable standing-code seed has been consumed this process.
# Exactly one durable read per printer per process (see :func:`seed_standing`).
_standing_seeded: set[int] = set()

# Generic in-memory rate ledger for :func:`allow`: (scope, key) -> last-allowed ts.
_allowed_at: dict[tuple[str, str], float] = {}

# A code must be absent at least this long before a reappearance re-notifies;
# anything shorter is one continuing incident. 600 s sits far above the observed
# 1-3 min flap gap, so the storm collapses to a single send, while a real
# clear-and-recur hours later still alerts. A code constant, not operator-tunable
# (like ams_presence._IDENTIFY_ACTIVE_S).
_HMS_RENOTIFY_ABSENT_SECONDS = 600.0


def hms_ledger_key(printer_id: int, code: str) -> str:
    """The durable ledger key for one printer's HMS ``code``.

    ``code`` is the LOSSLESS ``HMSError.full_code`` (16 hex chars from the
    ``hms[]`` path, 8 from ``print_error``). The pipeline used to key off
    ``f"{attr:08x}"`` alone, which COLLIDES for distinct codes sharing an attr —
    two different faults deduped as one, and no key that could address a durable
    row without ambiguity. Composed here so the seeding read and the send-side
    write can never drift apart.
    """
    return f"{printer_id}:{code}"


def new_codes(printer_id: int, current_codes: set[str], now: float) -> set[str]:
    """Return the subset of ``current_codes`` that should notify on this push.

    A code qualifies iff it was never seen for ``printer_id``, or it was last seen
    at least :data:`_HMS_RENOTIFY_ABSENT_SECONDS` ago (a genuine clear-and-recur).
    Side effects, in order:

    * the "fresh" set is computed against the PRIOR last-seen values;
    * every currently-present code has its last-seen bumped to ``now``;
    * any ledger entry older than the re-notify window is pruned inline — a code
      gone that long re-notifies as new anyway, so dropping it is
      behavior-preserving and keeps the per-printer map bounded to codes seen
      within the window (memory self-bounding).

    Per-printer isolation: each printer keeps its own independent code ledger.
    Codes pre-marked by :func:`seed_standing` are indistinguishable from codes
    seen live this process — that is exactly the intent.
    """
    seen = _last_seen.setdefault(printer_id, {})

    # Compute freshness BEFORE bumping, so a code present this push is judged
    # against when it was last seen, not against `now`.
    fresh = {
        code
        for code in current_codes
        if (last := seen.get(code)) is None or (now - last) >= _HMS_RENOTIFY_ABSENT_SECONDS
    }

    for code in current_codes:
        seen[code] = now

    # Prune absent-too-long entries. After the bump every current code is at `now`,
    # so only genuinely-absent codes fall below the cutoff.
    cutoff = now - _HMS_RENOTIFY_ABSENT_SECONDS
    for code in [c for c, ts in seen.items() if ts < cutoff]:
        del seen[code]
    if not seen:
        _last_seen.pop(printer_id, None)

    return fresh


def needs_standing_seed(printer_id: int) -> bool:
    """Whether :func:`seed_standing` still has work for ``printer_id``.

    Cheap in-memory predicate so the caller only opens a DB session on the one
    push per printer per process that actually needs the durable read.
    """
    return printer_id not in _standing_seeded


async def seed_standing(
    db: AsyncSession,
    printer_id: int,
    live_keys: Iterable[str],
    now: float,
) -> set[str]:
    """Pre-mark the printer's standing pre-restart HMS codes as already seen.

    Called on the FIRST status push per printer per process (the same one-shot
    idiom as ``spool_respool.note_status_push``), BEFORE :func:`new_codes` runs
    for that push. Returns the set of codes pre-marked (for logging/tests).

    Semantics:

    * a live code that HAS a durable ledger row was alerted on before the
      restart — it is one continuing, still-standing incident, so it is stamped
      into the in-memory ledger at ``now`` and :func:`new_codes` will NOT return
      it. This is what stops the deploy-time fleet-wide re-blast;
    * a live code with NO durable row is left untouched → it is new → it
      notifies. A fault that arose while the server was down must reach the
      operator.

    The one-shot is consumed even when the read finds nothing (or fails), so this
    is exactly one durable read per printer per process. The caller-supplied
    ``now`` keeps the stamp on the same clock :func:`new_codes` uses.

    NOTE the caller contract: seed only from a push carrying a REAL printer
    report. A fresh ``PrinterState`` broadcast at connect time (state
    ``"unknown"``, empty ``hms``) would consume the one-shot with an empty live
    set and let the standing codes re-blast on the next push — the same trap
    ``spool_respool.note_status_push`` guards against.
    """
    _standing_seeded.add(printer_id)
    live = {k for k in live_keys if k}
    if not live:
        return set()

    try:
        known = await load_keys(db, HMS_SCOPE, f"{printer_id}:")
    except SQLAlchemyError as exc:
        # Fail towards notifying: an unreadable ledger must never silence a fault.
        logger.warning("[NOTIFY-DEDUP] standing seed read failed for printer %s: %s", printer_id, exc)
        return set()

    standing = {code for code in live if hms_ledger_key(printer_id, code) in known}
    if standing:
        seen = _last_seen.setdefault(printer_id, {})
        for code in standing:
            seen[code] = now
        logger.info(
            "[NOTIFY-DEDUP] printer %s: %d standing HMS code(s) known from the durable ledger, not re-notified: %s",
            printer_id,
            len(standing),
            ", ".join(sorted(standing)),
        )
    return standing


async def load_keys(db: AsyncSession, scope: str, prefix: str = "") -> set[str]:
    """Return the durable ``dedup_key`` set for ``scope`` (optionally prefixed).

    The prefix filter keeps the per-printer seeding read to that printer's rows.
    """
    stmt = select(NotificationLedger.dedup_key).where(NotificationLedger.scope == scope)
    if prefix:
        stmt = stmt.where(NotificationLedger.dedup_key.startswith(prefix))
    result = await db.execute(stmt)
    return set(result.scalars().all())


async def record_sent(db: AsyncSession, scope: str, key: str, now: float | None = None) -> None:
    """Upsert "an alert for ``(scope, key)`` was just delivered".

    Idempotent by construction: re-recording the same key overwrites the stamp,
    it never inserts a second row (composite PK). Never raises — a ledger write
    failure must not break the notification path that just succeeded; the cost of
    a lost row is one extra alert after the next restart.
    """
    stamp = _as_naive_utc(now)
    try:
        row = await db.get(NotificationLedger, {"scope": scope, "dedup_key": key})
        if row is None:
            db.add(NotificationLedger(scope=scope, dedup_key=key, last_sent_at=stamp))
        else:
            row.last_sent_at = stamp
        await db.commit()
    except SQLAlchemyError as exc:
        # Includes the concurrent-insert IntegrityError: the row then already
        # exists with a near-identical stamp, which is the outcome we wanted.
        logger.warning("[NOTIFY-DEDUP] ledger write failed for %s/%s: %s", scope, key, exc)
        try:
            await db.rollback()
        except SQLAlchemyError:
            pass


async def prune_ledger(db: AsyncSession, older_than_days: int = LEDGER_PRUNE_DAYS, now: float | None = None) -> int:
    """Delete ledger rows untouched for ``older_than_days``; return the row count.

    Startup housekeeping (wired into main's lifespan beside the other hygiene
    passes). Behaviour-preserving: a key that old is outside every re-notify
    window, so it would notify as new whether or not its row survived.
    """
    cutoff = _as_naive_utc(now) - timedelta(days=older_than_days)
    result = await db.execute(delete(NotificationLedger).where(NotificationLedger.last_sent_at < cutoff))
    await db.commit()
    return int(result.rowcount or 0)


def allow(scope: str, key: str, now: float, window_s: float) -> bool:
    """Generic in-memory rate gate: True (and stamp) iff ``key`` may fire now.

    A key fires on its first call, then only once ``window_s`` has elapsed —
    boundary INCLUSIVE (elapsed exactly ``window_s`` is allowed), matching
    :func:`new_codes`. Denied calls do NOT extend the window, so a per-tick
    caller cannot starve itself forever.

    Deliberately in-memory only: this gate backs *standing-condition* events (a
    queue item held on filament) whose emitters already carry durable transition
    guards. One re-notify after a restart for a still-standing hold is acceptable
    and self-limiting — it is not the fleet-wide multi-code blast that made the
    HMS path need a durable ledger.
    """
    last = _allowed_at.get((scope, key))
    if last is not None and (now - last) < window_s:
        return False
    _allowed_at[(scope, key)] = now
    return True


def _as_naive_utc(now: float | None) -> datetime:
    """Wall-clock seconds → naive UTC datetime (the fork's column convention)."""
    if now is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    return datetime.fromtimestamp(now, tz=timezone.utc).replace(tzinfo=None)


def _reset_state() -> None:
    """Test hook: clear all module-level in-memory state between cases."""
    _last_seen.clear()
    _standing_seeded.clear()
    _allowed_at.clear()
