"""Unit tests for the per-code HMS re-notify dedup (hms_notify_dedup).

The module replaced main's per-printer set-replace + empty-list grace clear, which
re-notified on every reappearance of a code that flapped in/out while another code
stayed present (the production 0700_0002 storm: ~80 sends in 2.5 h). A code is
"new" only on first appearance or after being ABSENT past the re-notify window.
"""

import pytest

from backend.app.services import hms_notify_dedup
from backend.app.services.hms_notify_dedup import _HMS_RENOTIFY_ABSENT_SECONDS, new_codes

WINDOW = _HMS_RENOTIFY_ABSENT_SECONDS  # 600.0


@pytest.fixture(autouse=True)
def _reset_module_state():
    hms_notify_dedup._reset_state()
    yield
    hms_notify_dedup._reset_state()


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
    assert "old" in hms_notify_dedup._last_seen[1]
    # A later push with a different code: "old" is now absent 1000 s (> window) and
    # is pruned; only the current code remains.
    new_codes(1, {"new"}, 1000.0)
    assert "old" not in hms_notify_dedup._last_seen[1]
    assert set(hms_notify_dedup._last_seen[1]) == {"new"}


def test_empty_current_prunes_and_drops_empty_printer():
    """A push with no codes prunes stale entries and removes an emptied printer
    ledger (the function tolerates the empty-hms case main no longer calls with)."""
    new_codes(3, {"x"}, 0.0)
    assert 3 in hms_notify_dedup._last_seen
    new_codes(3, set(), 1000.0)  # x now absent > window → pruned → ledger emptied
    assert 3 not in hms_notify_dedup._last_seen


def test_within_window_entry_is_retained_not_pruned():
    """A code absent for LESS than the window keeps its entry so a return within the
    window is still recognized as the same continuing incident (not re-notified)."""
    new_codes(7, {"y"}, 0.0)
    new_codes(7, {"z"}, 100.0)  # y absent 100 s (< window) → retained
    assert "y" in hms_notify_dedup._last_seen[7]
    assert new_codes(7, {"y"}, 150.0) == set()  # y returns within window → not new


def test_reset_state_clears_all():
    """_reset_state wipes the ledger so a code reads new again."""
    new_codes(1, {"a"}, 0.0)
    assert hms_notify_dedup._last_seen
    hms_notify_dedup._reset_state()
    assert hms_notify_dedup._last_seen == {}
    assert new_codes(1, {"a"}, 1.0) == {"a"}  # new again after reset
