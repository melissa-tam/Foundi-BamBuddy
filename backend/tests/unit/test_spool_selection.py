"""Unit tests for the spool-selection policy module.

Covers the pure matcher (:func:`match_filaments_to_slots`), the AMS-Backup gate
(:func:`effective_policy`), the minimum-start floor, and the defaults-drift
guard tying the module constants to ``AppSettings``.
"""

import logging

from backend.app.schemas.settings import AppSettings
from backend.app.services.spool_selection import (
    DEFAULT_MIN_START_SPOOL_G,
    DEFAULT_SELECTION_POLICY,
    SELECTION_POLICIES,
    SlotInventory,
    effective_policy,
    match_filaments_to_slots,
)


def _loaded(gtid, *, ams_id=0, tray_id=None, ftype="PLA", color="#FF0000", tii="", remain=-1):
    """Build one loaded-filament dict as the scheduler's _build_loaded_filaments emits."""
    return {
        "type": ftype,
        "color": color,
        "tray_info_idx": tii,
        "ams_id": ams_id,
        "tray_id": tray_id if tray_id is not None else gtid,
        "global_tray_id": gtid,
        "is_external": ams_id < 0,
        "remain": remain,
    }


def _req(slot_id=1, *, ftype="PLA", color="#FF0000", tii="", used_grams=0.0):
    return {"slot_id": slot_id, "type": ftype, "color": color, "tray_info_idx": tii, "used_grams": used_grams}


def _match(required, loaded, *, policy, inv=None, backup_on=True, min_start_g=0):
    return match_filaments_to_slots(
        required, loaded, policy=policy, inv=inv or {}, backup_on=backup_on, min_start_g=min_start_g
    )


class TestConstantsMatchSettingsDefaults:
    def test_constants_equal_appsettings_defaults(self):
        """Guards against default drift between the module and the settings schema."""
        s = AppSettings()
        assert s.spool_selection_policy == DEFAULT_SELECTION_POLICY
        assert s.min_start_spool_g == DEFAULT_MIN_START_SPOOL_G
        assert DEFAULT_SELECTION_POLICY in SELECTION_POLICIES


class TestFirstLoadedFifo:
    def test_fifo_within_bucket_picks_oldest(self):
        """Two identical matching spools — the older first_loaded ordinal wins."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        inv = {
            0: SlotInventory(remaining_g=500.0, first_loaded_ord=100.0),
            1: SlotInventory(remaining_g=500.0, first_loaded_ord=200.0),
        }
        out = _match([_req()], loaded, policy="first_loaded", inv=inv)
        assert out.mapping == [0]
        assert out.start_blocked_slots == []

    def test_fifo_newer_first_still_picks_oldest(self):
        """Emission order newest-first: FIFO sort must still surface the oldest."""
        loaded = [_loaded(1, tray_id=1), _loaded(0, tray_id=0)]
        inv = {
            0: SlotInventory(remaining_g=500.0, first_loaded_ord=100.0),
            1: SlotInventory(remaining_g=500.0, first_loaded_ord=200.0),
        }
        out = _match([_req()], loaded, policy="first_loaded", inv=inv)
        assert out.mapping == [0]

    def test_unbound_trays_sort_last(self):
        """A spool with a known first-loaded ordinal beats an unbound tray."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        inv = {1: SlotInventory(remaining_g=500.0, first_loaded_ord=100.0)}  # only gtid 1 bound
        out = _match([_req()], loaded, policy="first_loaded", inv=inv)
        assert out.mapping == [1]


class TestBucketPrecedenceBeatsAge:
    def test_exact_color_beats_older_type_only(self):
        """An exact-colour match wins over an older but wrong-colour spool."""
        loaded = [
            _loaded(0, tray_id=0, color="#FF0000"),  # exact red, newer
            _loaded(1, tray_id=1, color="#0000FF"),  # blue (type-only), older
        ]
        inv = {
            0: SlotInventory(remaining_g=500.0, first_loaded_ord=200.0),
            1: SlotInventory(remaining_g=500.0, first_loaded_ord=100.0),
        }
        out = _match([_req(color="#FF0000")], loaded, policy="first_loaded", inv=inv)
        assert out.mapping == [0]


class TestSlotOrder:
    def test_slot_order_preserves_emission_order(self):
        """slot_order performs no sort — first emitted matching tray wins."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        out = _match([_req()], loaded, policy="slot_order")
        assert out.mapping == [0]

    def test_slot_order_second_slot_takes_next(self):
        """Two requirements consume two trays in emission order."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        out = _match([_req(1), _req(2)], loaded, policy="slot_order")
        assert out.mapping == [0, 1]


class TestEffectivePolicyBackupGate:
    def test_lowest_remaining_backup_off_degrades_to_slot_order(self):
        assert effective_policy("lowest_remaining", False) == "slot_order"

    def test_lowest_remaining_backup_on_passes_through(self):
        assert effective_policy("lowest_remaining", True) == "lowest_remaining"

    def test_lowest_remaining_backup_unknown_passes_through(self):
        assert effective_policy("lowest_remaining", None) == "lowest_remaining"

    def test_first_loaded_passes_through_regardless_of_backup(self):
        assert effective_policy("first_loaded", False) == "first_loaded"
        assert effective_policy("first_loaded", True) == "first_loaded"

    def test_invalid_policy_falls_back_to_default(self):
        assert effective_policy("bogus", None) == DEFAULT_SELECTION_POLICY
        assert effective_policy(None, None) == DEFAULT_SELECTION_POLICY


class TestLowestRemaining:
    def test_lowest_remaining_tracked_prefers_lower_grams(self):
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        inv = {
            0: SlotInventory(remaining_g=800.0, first_loaded_ord=None),
            1: SlotInventory(remaining_g=50.0, first_loaded_ord=None),
        }
        out = _match([_req()], loaded, policy="lowest_remaining", inv=inv)
        assert out.mapping == [1]

    def test_tracked_tier_beats_mqtt_tier(self):
        """Inventory-tracked (any grams) sorts before MQTT-only, regardless of value."""
        loaded = [
            _loaded(0, tray_id=0, remain=10),  # MQTT-only, low percentage
            _loaded(1, tray_id=1, remain=-1),  # tracked, high grams
        ]
        inv = {1: SlotInventory(remaining_g=800.0, first_loaded_ord=None)}
        out = _match([_req()], loaded, policy="lowest_remaining", inv=inv)
        assert out.mapping == [1]

    def test_mqtt_unknown_sorts_after_known(self):
        loaded = [_loaded(0, tray_id=0, remain=-1), _loaded(1, tray_id=1, remain=50)]
        out = _match([_req()], loaded, policy="lowest_remaining", inv={})
        assert out.mapping == [1]


class TestSmartCover:
    def test_backup_off_prefers_covering_over_older(self):
        """first_loaded, backup OFF: an older spool that can't finish the job yields
        to a newer one that can (smart-cover)."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        inv = {
            0: SlotInventory(remaining_g=200.0, first_loaded_ord=100.0),  # older, can't cover 300
            1: SlotInventory(remaining_g=500.0, first_loaded_ord=200.0),  # newer, covers
        }
        out = _match([_req(used_grams=300.0)], loaded, policy="first_loaded", inv=inv, backup_on=False)
        assert out.mapping == [1]

    def test_backup_on_is_pure_fifo(self):
        """Backup ON: smart-cover is skipped; the oldest wins even if it can't cover."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        inv = {
            0: SlotInventory(remaining_g=200.0, first_loaded_ord=100.0),
            1: SlotInventory(remaining_g=500.0, first_loaded_ord=200.0),
        }
        out = _match([_req(used_grams=300.0)], loaded, policy="first_loaded", inv=inv, backup_on=True)
        assert out.mapping == [0]

    def test_none_covering_oldest_wins(self):
        """Backup OFF and NO candidate can cover — the oldest is chosen anyway."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        inv = {
            0: SlotInventory(remaining_g=200.0, first_loaded_ord=100.0),
            1: SlotInventory(remaining_g=500.0, first_loaded_ord=200.0),
        }
        out = _match([_req(used_grams=900.0)], loaded, policy="first_loaded", inv=inv, backup_on=False)
        assert out.mapping == [0]

    def test_backup_none_prefers_covering(self):
        """Backup unknown (None) is treated like OFF for smart-cover."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        inv = {
            0: SlotInventory(remaining_g=200.0, first_loaded_ord=100.0),
            1: SlotInventory(remaining_g=500.0, first_loaded_ord=200.0),
        }
        out = _match([_req(used_grams=300.0)], loaded, policy="first_loaded", inv=inv, backup_on=None)
        assert out.mapping == [1]


class TestMinStartFloor:
    def test_drops_known_low_keeps_unknown_eligible(self):
        """A known below-floor spool is skipped; an unknown/unbound tray is used."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        inv = {0: SlotInventory(remaining_g=50.0, first_loaded_ord=None)}  # gtid 1 unknown
        out = _match([_req()], loaded, policy="slot_order", inv=inv, min_start_g=120)
        assert out.mapping == [1]
        assert out.start_blocked_slots == []

    def test_known_above_floor_stays_eligible(self):
        loaded = [_loaded(0, tray_id=0)]
        inv = {0: SlotInventory(remaining_g=500.0, first_loaded_ord=None)}
        out = _match([_req()], loaded, policy="slot_order", inv=inv, min_start_g=120)
        assert out.mapping == [0]
        assert out.start_blocked_slots == []


class TestStartBlockedSlots:
    def test_start_blocked_when_dropped_would_have_matched(self):
        """Only matching spool is below the floor → slot is start-blocked, no mapping."""
        loaded = [_loaded(0, tray_id=0, color="#FF0000")]
        inv = {0: SlotInventory(remaining_g=50.0, first_loaded_ord=None)}
        out = _match([_req(color="#FF0000")], loaded, policy="slot_order", inv=inv, min_start_g=120)
        assert out.mapping == [-1]
        assert out.start_blocked_slots == [1]

    def test_not_start_blocked_when_dropped_would_not_match(self):
        """A below-floor spool of the WRONG type is a plain no-match, not a start-block."""
        loaded = [_loaded(0, tray_id=0, ftype="PETG")]
        inv = {0: SlotInventory(remaining_g=50.0, first_loaded_ord=None)}
        out = _match([_req(ftype="PLA")], loaded, policy="slot_order", inv=inv, min_start_g=120)
        assert out.mapping == [-1]
        assert out.start_blocked_slots == []

    def test_not_start_blocked_when_eligible_match_exists(self):
        """When an eligible spool matches, a below-floor sibling does not raise a block."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        inv = {
            0: SlotInventory(remaining_g=50.0, first_loaded_ord=None),  # below floor
            1: SlotInventory(remaining_g=500.0, first_loaded_ord=None),  # eligible
        }
        out = _match([_req()], loaded, policy="slot_order", inv=inv, min_start_g=120)
        assert out.mapping == [1]
        assert out.start_blocked_slots == []

    def test_floor_disabled_never_blocks(self):
        """min_start_g == 0 disables the floor entirely."""
        loaded = [_loaded(0, tray_id=0)]
        inv = {0: SlotInventory(remaining_g=50.0, first_loaded_ord=None)}
        out = _match([_req()], loaded, policy="slot_order", inv=inv, min_start_g=0)
        assert out.mapping == [0]
        assert out.start_blocked_slots == []


class TestOutOfRotation:
    def test_oor_never_matches_even_as_only_candidate(self):
        """A jammed spool is invisible to selection: the only same-type spool
        being out-of-rotation yields a no-match, not a pick."""
        loaded = [_loaded(0, tray_id=0, color="#FF0000")]
        inv = {0: SlotInventory(remaining_g=500.0, first_loaded_ord=100.0, out_of_rotation=True)}
        out = _match([_req(color="#FF0000")], loaded, policy="first_loaded", inv=inv)
        assert out.mapping == [-1]
        assert out.start_blocked_slots == []

    def test_oor_never_start_blocked_distinct_from_floor_drop(self):
        """An out-of-rotation spool ABOVE the floor is fully excluded — it must
        NOT land in start_blocked_slots (that path is only for floor-dropped
        candidates that would otherwise have matched)."""
        loaded = [_loaded(0, tray_id=0, color="#FF0000")]
        # remaining_g well above the floor, so the ONLY reason it is gone is OOR.
        inv = {0: SlotInventory(remaining_g=500.0, first_loaded_ord=None, out_of_rotation=True)}
        out = _match([_req(color="#FF0000")], loaded, policy="slot_order", inv=inv, min_start_g=120)
        assert out.mapping == [-1]
        assert out.start_blocked_slots == []

    def test_default_false_preserves_behavior(self):
        """out_of_rotation defaults to False — an existing-style FIFO happy case
        (constructed without the flag) still picks the oldest spool unchanged."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        inv = {
            0: SlotInventory(remaining_g=500.0, first_loaded_ord=100.0),
            1: SlotInventory(remaining_g=500.0, first_loaded_ord=200.0),
        }
        out = _match([_req()], loaded, policy="first_loaded", inv=inv)
        assert out.mapping == [0]
        assert out.start_blocked_slots == []

    def test_oor_excluded_then_fifo_picks_older_eligible(self):
        """Two eligible spools plus an OLDER out-of-rotation spool: the jammed
        oldest is skipped and FIFO picks the older of the two ELIGIBLE ones."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1), _loaded(2, tray_id=2)]
        inv = {
            0: SlotInventory(remaining_g=500.0, first_loaded_ord=50.0, out_of_rotation=True),  # oldest, jammed
            1: SlotInventory(remaining_g=500.0, first_loaded_ord=100.0),  # older eligible
            2: SlotInventory(remaining_g=500.0, first_loaded_ord=200.0),  # newer eligible
        }
        out = _match([_req()], loaded, policy="first_loaded", inv=inv)
        assert out.mapping == [1]
        assert out.start_blocked_slots == []


class TestSpentExclusion:
    """A spent spool (``SlotInventory.spent``) is hard-excluded exactly like an
    out-of-rotation one — regardless of policy or floor — so a run-dry roll can
    never start a print. Kept SEPARATE from out_of_rotation (log semantics differ)."""

    def test_spent_excluded_under_slot_order_floor_zero(self):
        """The exact gap S2 closes: slot_order + min_start_g=0 (the skip_filament_check
        path) still hard-excludes a spent spool — the ONLY matching candidate being
        spent yields a no-match, not a start."""
        loaded = [_loaded(0, tray_id=0, color="#FF0000")]
        inv = {0: SlotInventory(remaining_g=500.0, first_loaded_ord=None, spent=True)}
        out = _match([_req(color="#FF0000")], loaded, policy="slot_order", inv=inv, min_start_g=0)
        assert out.mapping == [-1]
        assert out.start_blocked_slots == []

    def test_spent_never_start_blocked(self):
        """A spent spool is fully excluded — it must NOT surface in
        start_blocked_slots (that path is only for floor-dropped candidates)."""
        loaded = [_loaded(0, tray_id=0, color="#FF0000")]
        inv = {0: SlotInventory(remaining_g=500.0, first_loaded_ord=None, spent=True)}
        out = _match([_req(color="#FF0000")], loaded, policy="slot_order", inv=inv, min_start_g=120)
        assert out.mapping == [-1]
        assert out.start_blocked_slots == []

    def test_oor_and_spent_both_excluded_trace_names_reason(self, caplog):
        """OOR and spent slots are both excluded and the decision trace names WHICH
        reason each slot vanished for (excluded_oor vs excluded_spent), leaving the
        live slot to be picked."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1), _loaded(2, tray_id=2)]
        inv = {
            0: SlotInventory(remaining_g=500.0, first_loaded_ord=50.0, out_of_rotation=True),  # jammed
            1: SlotInventory(remaining_g=500.0, first_loaded_ord=100.0, spent=True),  # run-dry
            2: SlotInventory(remaining_g=500.0, first_loaded_ord=200.0),  # live
        }
        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_selection"):
            out = _match([_req()], loaded, policy="first_loaded", inv=inv)
        assert out.mapping == [2]  # only the live slot is selectable
        assert out.start_blocked_slots == []
        trace = "\n".join(r.message for r in caplog.records)
        assert "excluded_oor=[0]" in trace
        assert "excluded_spent=[1]" in trace

    def test_spent_false_default_preserves_behavior(self):
        """spent defaults to False — a plain FIFO happy case (no flag set) still
        picks the oldest spool unchanged."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        inv = {
            0: SlotInventory(remaining_g=500.0, first_loaded_ord=100.0),
            1: SlotInventory(remaining_g=500.0, first_loaded_ord=200.0),
        }
        assert inv[0].spent is False  # default
        out = _match([_req()], loaded, policy="first_loaded", inv=inv)
        assert out.mapping == [0]
        assert out.start_blocked_slots == []
