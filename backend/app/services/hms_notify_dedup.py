"""Per-code HMS re-notify dedup — collapse a flapping HMS code to one alert.

Production incident (2026-07-18): HMS code ``0700_0002`` flapped in and out of a
printer's ``hms`` list ~80 times in 2.5 h (every 1-3 min), and each RETURN
re-notified — every send also capturing a camera snapshot. The mechanism was
``main.on_printer_status_change``'s dedup: it REPLACED the whole per-printer
notified set on every push (``_notified_hms_errors[printer_id] = current_codes``)
and a 30 s grace clear that only ran when the ``hms`` list went FULLY empty. A
code flapping out while *another* code stayed present never hit the grace path,
so every reappearance counted as new.

This module replaces that with a per-code last-seen ledger. A code is "new"
(worth notifying) only on its FIRST appearance for a printer, or after it has
been ABSENT at least :data:`_HMS_RENOTIFY_ABSENT_SECONDS`. Any window well above
the observed 1-3 min flap gap collapses the storm to exactly one notification,
while a genuine clear-and-recur hours later still re-notifies.

State is process-lifetime and in-memory, matching the fork's other event-edge
bookkeeping (``ams_presence._last_presence``, ``spool_respool._last_tray_now``);
it is lost on restart, where a still-live code correctly notifies once more.
"""

from __future__ import annotations

# printer_id -> {attr-hex code -> last-seen wall-clock timestamp}. Wall clock
# (time.time()) to match the caller in main.on_printer_status_change; the value is
# whatever monotonic-or-wall ``now`` the caller passes, used only for elapsed math.
_last_seen: dict[int, dict[str, float]] = {}

# A code must be absent at least this long before a reappearance re-notifies;
# anything shorter is one continuing incident. 600 s sits far above the observed
# 1-3 min flap gap, so the storm collapses to a single send, while a real
# clear-and-recur hours later still alerts. A code constant, not operator-tunable
# (like ams_presence._IDENTIFY_ACTIVE_S).
_HMS_RENOTIFY_ABSENT_SECONDS = 600.0


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


def _reset_state() -> None:
    """Test hook: clear all module-level last-seen state between cases."""
    _last_seen.clear()
