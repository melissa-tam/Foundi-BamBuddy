"""Unit tests for the notification dedup service (notify_dedup).

Two layers under test:

* the in-memory per-code HMS ledger, which replaced main's per-printer
  set-replace + empty-list grace clear (that re-notified on every reappearance of
  a code flapping in/out while another code stayed present — the production
  0700_0002 storm: ~80 sends in 2.5 h). A code is "new" only on first appearance
  or after being ABSENT past the re-notify window;
* the DURABLE ledger (Phase D), which stops a standing pre-restart code from
  being re-blasted at every deploy (2026-07-20 00:45: six printers re-announced
  their standing codes within 7 s) WITHOUT silencing a fault that arose while the
  server was down.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.app.models.notification_ledger import NotificationLedger
from backend.app.services import notify_dedup
from backend.app.services.notify_dedup import (
    _HMS_RENOTIFY_ABSENT_SECONDS,
    HMS_SCOPE,
    allow,
    hms_ledger_key,
    load_keys,
    new_codes,
    prune_ledger,
    record_sent,
    seed_standing,
)

WINDOW = _HMS_RENOTIFY_ABSENT_SECONDS  # 600.0


@pytest.fixture(autouse=True)
def _reset_module_state():
    notify_dedup._reset_state()
    yield
    notify_dedup._reset_state()


def test_first_appearance_notifies():
    """A never-before-seen code is returned as new."""
    assert new_codes(1, {"0700_0002"}, 0.0) == {"0700_0002"}


def test_second_distinct_code_notifies_once():
    """A NEW code appearing alongside an already-seen one is the only fresh one."""
    assert new_codes(1, {"0700_0002"}, 0.0) == {"0700_0002"}
    # 0700_0002 still present (continuing) + a genuinely new 0300_8010 arrives.
    assert new_codes(1, {"0700_0002", "0300_8010"}, 60.0) == {"0300_8010"}


def test_two_minute_flap_cycle_collapses_to_one_notification():
    """The 0700_0002 storm: a code flapping out-then-back every 2 min re-notifies
    exactly once across the whole incident (every gap is < the 600 s window)."""
    fires = 0
    # 30 present-pushes spaced 120 s apart, with the code absent in between. Only
    # the very first appearance should be "new".
    for i in range(30):
        now = i * 120.0
        # Absent push between appearances (a DIFFERENT code present) does not bump
        # our code's last-seen — models hms:[other] during the gap.
        if i:
            new_codes(1, {"0300_0001"}, now - 60.0)
        if new_codes(1, {"0700_0002"}, now):
            fires += 1
    assert fires == 1


def test_absent_at_least_window_renotifies():
    """A code gone for >= the re-notify window is a genuine clear-and-recur and
    notifies again; gone for < the window is one continuing incident."""
    assert new_codes(1, {"0700_0002"}, 0.0) == {"0700_0002"}  # first
    # Returns just under the window → NOT new.
    assert new_codes(1, {"0700_0002"}, WINDOW - 1) == set()
    # Bumped to WINDOW-1; returns exactly WINDOW later (elapsed == WINDOW) → new.
    assert new_codes(1, {"0700_0002"}, (WINDOW - 1) + WINDOW) == {"0700_0002"}


def test_boundary_just_below_window_is_not_new():
    """Elapsed strictly below the window does not re-notify (inclusive at ==)."""
    assert new_codes(5, {"c"}, 1000.0) == {"c"}
    assert new_codes(5, {"c"}, 1000.0 + WINDOW - 0.001) == set()


def test_per_printer_isolation():
    """Each printer keeps its own ledger — a code seen on one is still new on another."""
    assert new_codes(1, {"0700_0002"}, 0.0) == {"0700_0002"}
    assert new_codes(1, {"0700_0002"}, 30.0) == set()  # continuing on printer 1
    assert new_codes(2, {"0700_0002"}, 30.0) == {"0700_0002"}  # first time on printer 2


def test_pruning_bounds_memory():
    """An entry absent past the window is pruned inline on the next call, so the
    per-printer map does not accumulate codes that will never dedup again."""
    new_codes(1, {"old"}, 0.0)
    assert "old" in notify_dedup._last_seen[1]
    # A later push with a different code: "old" is now absent 1000 s (> window) and
    # is pruned; only the current code remains.
    new_codes(1, {"new"}, 1000.0)
    assert "old" not in notify_dedup._last_seen[1]
    assert set(notify_dedup._last_seen[1]) == {"new"}


def test_empty_current_prunes_and_drops_empty_printer():
    """A push with no codes prunes stale entries and removes an emptied printer
    ledger (the function tolerates the empty-hms case main no longer calls with)."""
    new_codes(3, {"x"}, 0.0)
    assert 3 in notify_dedup._last_seen
    new_codes(3, set(), 1000.0)  # x now absent > window → pruned → ledger emptied
    assert 3 not in notify_dedup._last_seen


def test_within_window_entry_is_retained_not_pruned():
    """A code absent for LESS than the window keeps its entry so a return within the
    window is still recognized as the same continuing incident (not re-notified)."""
    new_codes(7, {"y"}, 0.0)
    new_codes(7, {"z"}, 100.0)  # y absent 100 s (< window) → retained
    assert "y" in notify_dedup._last_seen[7]
    assert new_codes(7, {"y"}, 150.0) == set()  # y returns within window → not new


def test_reset_state_clears_all():
    """_reset_state wipes the ledger so a code reads new again."""
    new_codes(1, {"a"}, 0.0)
    assert notify_dedup._last_seen
    notify_dedup._reset_state()
    assert notify_dedup._last_seen == {}
    assert new_codes(1, {"a"}, 1.0) == {"a"}  # new again after reset


class TestHmsLedgerKey:
    """The durable key must be lossless AND printer-scoped."""

    def test_distinct_full_codes_sharing_an_attr_are_distinct_keys(self):
        """The collision the old `f"{attr:08x}"` key had: two different faults on
        one attr (a failed read vs a runout on the same AMS slot) produced ONE key,
        so the second never notified and no durable row could address either."""
        attr = 0x07002000
        read_fail = f"{attr:08X}00010081"
        runout = f"{attr:08X}00020001"
        assert hms_ledger_key(4, read_fail) != hms_ledger_key(4, runout)

    def test_same_code_on_two_printers_is_two_keys(self):
        assert hms_ledger_key(4, "AABB") != hms_ledger_key(5, "AABB")

    def test_new_codes_tracks_attr_siblings_separately(self):
        """End-to-end of the collision fix at the dedup layer: with full_code keys,
        the second fault on the same attr is its own incident and notifies."""
        attr = 0x07002000
        read_fail = f"{attr:08X}00010081"
        runout = f"{attr:08X}00020001"
        assert new_codes(4, {read_fail}, 0.0) == {read_fail}
        assert new_codes(4, {read_fail, runout}, 5.0) == {runout}


class TestAllowWindow:
    """The generic in-memory rate gate backing the queue-waiting chokepoint."""

    def test_first_call_allows(self):
        assert allow("queue_waiting", "12:filament_short", 0.0, 3600.0) is True

    def test_second_call_within_window_denied(self):
        assert allow("s", "k", 0.0, 100.0) is True
        assert allow("s", "k", 99.999, 100.0) is False

    def test_boundary_at_exactly_the_window_is_allowed(self):
        """Inclusive at ==, matching new_codes' re-notify boundary."""
        assert allow("s", "k", 0.0, 100.0) is True
        assert allow("s", "k", 100.0, 100.0) is True

    def test_denied_call_does_not_extend_the_window(self):
        """A per-tick caller must not starve itself: only ALLOWED calls stamp."""
        assert allow("s", "k", 0.0, 100.0) is True
        for t in (10.0, 40.0, 90.0):
            assert allow("s", "k", t, 100.0) is False
        assert allow("s", "k", 100.0, 100.0) is True

    def test_keys_and_scopes_are_independent(self):
        assert allow("s", "k1", 0.0, 100.0) is True
        assert allow("s", "k2", 0.0, 100.0) is True
        assert allow("other", "k1", 0.0, 100.0) is True


@pytest.mark.asyncio
class TestDurableLedger:
    """record_sent / load_keys / prune_ledger against a real SQLite session."""

    async def test_record_sent_inserts_then_upserts_idempotently(self, db_session):
        await record_sent(db_session, HMS_SCOPE, "1:AABB", 1_000.0)
        await record_sent(db_session, HMS_SCOPE, "1:AABB", 2_000.0)

        rows = (await db_session.execute(select(NotificationLedger))).scalars().all()
        assert len(rows) == 1  # upsert, never a second row
        assert rows[0].scope == HMS_SCOPE
        assert rows[0].dedup_key == "1:AABB"
        expected = datetime.fromtimestamp(2_000.0, tz=timezone.utc).replace(tzinfo=None)
        assert rows[0].last_sent_at == expected

    async def test_load_keys_filters_by_scope_and_prefix(self, db_session):
        await record_sent(db_session, HMS_SCOPE, "1:AA", 0.0)
        await record_sent(db_session, HMS_SCOPE, "1:BB", 0.0)
        await record_sent(db_session, HMS_SCOPE, "2:AA", 0.0)
        await record_sent(db_session, "other", "1:CC", 0.0)

        assert await load_keys(db_session, HMS_SCOPE, "1:") == {"1:AA", "1:BB"}
        assert await load_keys(db_session, HMS_SCOPE) == {"1:AA", "1:BB", "2:AA"}
        assert await load_keys(db_session, "other") == {"1:CC"}

    async def test_prune_deletes_only_rows_past_the_window(self, db_session):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        db_session.add_all(
            [
                NotificationLedger(scope=HMS_SCOPE, dedup_key="old", last_sent_at=now - timedelta(days=31)),
                NotificationLedger(scope=HMS_SCOPE, dedup_key="edge", last_sent_at=now - timedelta(days=29)),
                NotificationLedger(scope=HMS_SCOPE, dedup_key="fresh", last_sent_at=now),
            ]
        )
        await db_session.commit()

        deleted = await prune_ledger(db_session)

        assert deleted == 1
        assert await load_keys(db_session, HMS_SCOPE) == {"edge", "fresh"}

    async def test_prune_on_empty_table_is_a_no_op(self, db_session):
        assert await prune_ledger(db_session) == 0


@pytest.mark.asyncio
class TestSeedStanding:
    """The restart-replay fix: standing = known, downtime-raised = still alerts."""

    _STANDING = "0700200000010081"
    _NEW = "0700821000008010"

    async def test_standing_code_with_a_ledger_row_never_renotifies(self, db_session):
        """The deploy re-blast: a code we alerted on before the restart is one
        continuing incident, so the first post-restart push sends nothing."""
        await record_sent(db_session, HMS_SCOPE, hms_ledger_key(4, self._STANDING), 0.0)
        notify_dedup._reset_state()  # simulate the process restart (memory lost)

        seeded = await seed_standing(db_session, 4, {self._STANDING}, 1_000.0)

        assert seeded == {self._STANDING}
        assert new_codes(4, {self._STANDING}, 1_000.0) == set()

    async def test_code_raised_during_downtime_still_notifies(self, db_session):
        """No durable row ⇒ the operator was never told ⇒ it must alert, even
        though it is live at the very first push."""
        seeded = await seed_standing(db_session, 4, {self._NEW}, 1_000.0)

        assert seeded == set()
        assert new_codes(4, {self._NEW}, 1_000.0) == {self._NEW}

    async def test_mixed_push_seeds_only_the_known_code(self, db_session):
        await record_sent(db_session, HMS_SCOPE, hms_ledger_key(4, self._STANDING), 0.0)
        notify_dedup._reset_state()

        await seed_standing(db_session, 4, {self._STANDING, self._NEW}, 1_000.0)

        assert new_codes(4, {self._STANDING, self._NEW}, 1_000.0) == {self._NEW}

    async def test_seed_is_one_shot_per_printer(self, db_session):
        """Exactly one durable read per printer per process — the caller's
        needs_standing_seed() guard flips on the first seed."""
        assert notify_dedup.needs_standing_seed(4) is True
        await seed_standing(db_session, 4, set(), 0.0)
        assert notify_dedup.needs_standing_seed(4) is False
        assert notify_dedup.needs_standing_seed(5) is True  # per-printer

    async def test_seeded_standing_code_still_renotifies_after_a_real_clear(self, db_session):
        """Seeding pre-marks; it does not pin forever. A code that then stays away
        past the window is a genuine clear-and-recur and alerts again."""
        await record_sent(db_session, HMS_SCOPE, hms_ledger_key(4, self._STANDING), 0.0)
        notify_dedup._reset_state()
        await seed_standing(db_session, 4, {self._STANDING}, 1_000.0)
        assert new_codes(4, {self._STANDING}, 1_000.0) == set()

        assert new_codes(4, {self._STANDING}, 1_000.0 + WINDOW) == {self._STANDING}

    async def test_other_printers_rows_do_not_seed_this_printer(self, db_session):
        """A standing code on printer 4 must not silence the same code's FIRST
        appearance on printer 5."""
        await record_sent(db_session, HMS_SCOPE, hms_ledger_key(4, self._STANDING), 0.0)
        notify_dedup._reset_state()

        seeded = await seed_standing(db_session, 5, {self._STANDING}, 1_000.0)

        assert seeded == set()
        assert new_codes(5, {self._STANDING}, 1_000.0) == {self._STANDING}
