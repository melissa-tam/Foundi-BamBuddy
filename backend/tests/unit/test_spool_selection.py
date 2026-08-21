"""Unit tests for the spool-selection policy module.

Covers the pure matcher (:func:`match_filaments_to_slots`), the AMS-Backup gate
(:func:`effective_policy`), the minimum-start floor, and the defaults-drift
guard tying the module constants to ``AppSettings``.
"""

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.models.spool import Spool
from backend.app.schemas.settings import AppSettings
from backend.app.services.spool_selection import (
    DEFAULT_MIN_START_SPOOL_G,
    DEFAULT_SELECTION_POLICY,
    SELECTION_POLICIES,
    START_BLOCK_BELOW_FLOOR,
    START_BLOCK_UNKNOWN_GRAMS,
    MatchOutcome,
    SlotInventory,
    build_slot_inventory,
    dominant_start_block,
    effective_policy,
    filament_type_of,
    filament_types_match,
    match_filaments_to_slots,
    parse_pins,
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


def _match(required, loaded, *, policy, inv=None, backup_on=True, min_start_g=0, require_known_grams=False, pins=None):
    return match_filaments_to_slots(
        required,
        loaded,
        policy=policy,
        inv=inv or {},
        backup_on=backup_on,
        min_start_g=min_start_g,
        require_known_grams=require_known_grams,
        pins=pins,
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
    def test_drops_known_low_prefers_priced_over_unknown(self):
        """A known below-floor spool is skipped. The unbound tray beside it is only
        usable under the mid-print reading (``require_known_grams`` off); the START
        lane reserves it too — see TestUnknownGramsFailsClosed."""
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


class TestRowT10SubFloorDonor:
    """§4.1 row T10 — a sub-floor roll left seated as a firmware backup donor: **KEEP**.

    The row's verdict is "never pulled, never re-decided", and the two halves of that are
    owned by different modules. This class pins the SELECTION half: the same 90 g roll is
    refused by the START lane (``require_known_grams`` on, the full
    ``min_start_spool_g`` floor) and accepted by the MID-PRINT lane, where
    ``spool_recovery`` lowers the floor to its hard minimum past the protected layers —
    a low-but-not-empty roll is a valid replacement, a known-empty one never is.

    Read the pair as one statement: "below the floor" is a fact about STARTING a print,
    not about the roll's fitness or its identity. The binding half — no release, no mint,
    no write — is
    ``services/test_slot_pipeline.py::test_T10_a_sub_floor_roll_left_seated_is_never_re_decided``.
    """

    SUB_FLOOR_G = 90.0  # under the 150 g start floor, far above the 5 g replacement floor

    def _sub_floor_only(self):
        loaded = [_loaded(0, tray_id=0, color="#FF0000")]
        inv = {0: SlotInventory(remaining_g=self.SUB_FLOOR_G, first_loaded_ord=100.0)}
        return loaded, inv

    def test_the_start_lane_refuses_it_and_names_the_floor(self):
        loaded, inv = self._sub_floor_only()
        out = _match(
            [_req(color="#FF0000")],
            loaded,
            policy="first_loaded",
            inv=inv,
            min_start_g=DEFAULT_MIN_START_SPOOL_G,
            require_known_grams=True,
        )
        assert out.mapping == [-1], "no print STARTS on it"
        assert out.start_blocked_slots == [1]
        assert out.start_block_kinds == {1: START_BLOCK_BELOW_FLOOR}

    def test_the_mid_print_replacement_lane_still_takes_it(self):
        """The liveness half. Without it, "the floor holds" and "the roll is unusable"
        look identical — and the doctrine sentence T10 encodes is that they differ."""
        from backend.app.services.spool_recovery import _RECOVERY_HARD_MIN_G

        loaded, inv = self._sub_floor_only()
        out = _match(
            [_req(color="#FF0000")],
            loaded,
            policy="first_loaded",
            inv=inv,
            min_start_g=_RECOVERY_HARD_MIN_G,
        )
        assert out.mapping == [0], "still a donor mid-print"
        assert out.start_blocked_slots == []
        assert _RECOVERY_HARD_MIN_G < self.SUB_FLOOR_G < DEFAULT_MIN_START_SPOOL_G, (
            "the fixture must sit between the two floors, or the pair proves nothing"
        )

    def test_a_genuinely_empty_roll_is_refused_by_both(self):
        """The other side of the replacement floor: 'low' is a donor, 'empty' is not."""
        from backend.app.services.spool_recovery import _RECOVERY_HARD_MIN_G

        loaded = [_loaded(0, tray_id=0, color="#FF0000")]
        inv = {0: SlotInventory(remaining_g=2.0, first_loaded_ord=100.0)}
        out = _match([_req(color="#FF0000")], loaded, policy="first_loaded", inv=inv, min_start_g=_RECOVERY_HARD_MIN_G)
        assert out.mapping == [-1]
        assert out.start_blocked_slots == [1]


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


class TestUnknownGramsFailsClosed:
    """The START reading of the floor (``require_known_grams=True``): a candidate the
    ledger cannot price is reserved, never started. Prod trace 2026-08-07 23:38:13
    had exactly such a candidate (``inv_g=None`` beside ``remain=46``) sitting in
    ``eligible`` — a roll that may hold 5 g was startable.

    Every case here keeps at least one PRICED slot on the printer: the reading is
    scoped to a ledger that demonstrably speaks (see TestNoLedgerStaysLive)."""

    # A priced roll of another material: it makes the ledger speak without ever
    # matching the PLA requirement, so each case turns purely on the unpriced roll.
    PRICED_OTHER = 9

    @staticmethod
    def _priced_other():
        return _loaded(TestUnknownGramsFailsClosed.PRICED_OTHER, tray_id=3, ftype="PETG")

    @staticmethod
    def _priced_other_inv():
        return {TestUnknownGramsFailsClosed.PRICED_OTHER: SlotInventory(remaining_g=800.0, first_loaded_ord=1.0)}

    def test_unknown_grams_candidate_is_start_blocked(self):
        loaded = [_loaded(0, tray_id=0, color="#FF0000"), self._priced_other()]
        out = _match(
            [_req(color="#FF0000")],
            loaded,
            policy="slot_order",
            inv=self._priced_other_inv(),
            min_start_g=150,
            require_known_grams=True,
        )
        assert out.mapping == [-1]  # never started
        assert out.start_blocked_slots == [1]
        assert out.start_block_kinds == {1: START_BLOCK_UNKNOWN_GRAMS}
        assert out.start_block_kind == START_BLOCK_UNKNOWN_GRAMS

    def test_bound_row_without_grams_is_start_blocked(self):
        """Unknown is a property of the GRAMS, not of the binding: a bound slot whose
        remaining nothing can quote (Spoolman row without a weight) blocks too."""
        loaded = [_loaded(0, tray_id=0), self._priced_other()]
        inv = {0: SlotInventory(remaining_g=None, first_loaded_ord=100.0, ord_src="first_used")}
        inv.update(self._priced_other_inv())
        out = _match([_req()], loaded, policy="first_loaded", inv=inv, min_start_g=150, require_known_grams=True)
        assert out.start_block_kinds == {1: START_BLOCK_UNKNOWN_GRAMS}

    def test_known_good_candidate_still_wins(self):
        """The gate only removes the unpriceable roll — a priced, above-floor sibling
        starts the print exactly as before, and raises no block."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        inv = {1: SlotInventory(remaining_g=500.0, first_loaded_ord=100.0)}  # gtid 0 unpriceable
        out = _match([_req()], loaded, policy="slot_order", inv=inv, min_start_g=150, require_known_grams=True)
        assert out.mapping == [1]
        assert out.start_blocked_slots == []

    def test_mixed_reserves_word_as_below_floor(self):
        """One reserve of each kind: the hold speaks for the roll the farm HAS priced
        (``dominant_start_block``), because "below minimum" is true of it."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        inv = {0: SlotInventory(remaining_g=50.0, first_loaded_ord=None)}  # gtid 1 unpriceable
        out = _match([_req()], loaded, policy="slot_order", inv=inv, min_start_g=150, require_known_grams=True)
        assert out.mapping == [-1]
        assert out.start_block_kinds == {1: START_BLOCK_BELOW_FLOOR}

    def test_unknown_of_wrong_type_is_a_plain_no_match(self):
        """The reserve only raises a block when it WOULD have matched — an unpriceable
        roll of the wrong material is an ordinary no-match, not a start-block."""
        loaded = [_loaded(0, tray_id=0, ftype="ASA"), self._priced_other()]
        out = _match(
            [_req(ftype="PLA")],
            loaded,
            policy="slot_order",
            inv=self._priced_other_inv(),
            min_start_g=150,
            require_known_grams=True,
        )
        assert out.mapping == [-1]
        assert out.start_blocked_slots == []

    def test_floor_disabled_disables_the_unknown_gate_too(self):
        """``min_start_g == 0`` (Print Anyway / floor off) is the one escape hatch, and
        it must disable BOTH readings — otherwise an acknowledged job can never start."""
        loaded = [_loaded(0, tray_id=0), self._priced_other()]
        out = _match(
            [_req()],
            loaded,
            policy="slot_order",
            inv=self._priced_other_inv(),
            min_start_g=0,
            require_known_grams=True,
        )
        assert out.mapping == [0]
        assert out.start_blocked_slots == []

    def test_mid_print_reading_keeps_unknown_eligible(self):
        """Default OFF is spool_recovery's mid-print donor search: a refill must be able
        to feed a live print from a roll the ledger cannot price, even on a printer
        whose ledger speaks for its other slots."""
        loaded = [_loaded(0, tray_id=0), self._priced_other()]
        out = _match([_req()], loaded, policy="slot_order", inv=self._priced_other_inv(), min_start_g=5)
        assert out.mapping == [0]
        assert out.start_blocked_slots == []

    def test_trace_tells_the_two_reserves_apart(self, caplog):
        """Triage must distinguish "too light" from "unpriced" in the decision trace —
        the prod incident was invisible because both would have read ``dropped=``."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        inv = {0: SlotInventory(remaining_g=50.0, first_loaded_ord=None)}  # gtid 1 unpriceable
        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_selection"):
            _match([_req()], loaded, policy="slot_order", inv=inv, min_start_g=150, require_known_grams=True)
        trace = "\n".join(r.message for r in caplog.records)
        assert "'gtid': 0" in trace.split("dropped_below_floor=")[1].split("dropped_unknown_grams=")[0]
        assert "'gtid': 1" in trace.split("dropped_unknown_grams=")[1].split("excluded_oor=")[0]
        assert f"START-BLOCKED reason={START_BLOCK_BELOW_FLOOR}" in trace

    def test_hard_excluded_spool_never_reads_as_unknown(self):
        """A spent / jammed / archived row leaves before the floor runs, so it can never
        surface as an unknown-grams block (the exclusions stay tellable apart)."""
        loaded = [_loaded(0, tray_id=0, color="#FF0000")]
        inv = {0: SlotInventory(remaining_g=None, first_loaded_ord=None, spent=True)}
        out = _match(
            [_req(color="#FF0000")], loaded, policy="slot_order", inv=inv, min_start_g=150, require_known_grams=True
        )
        assert out.mapping == [-1]
        assert out.start_blocked_slots == []


class TestNoLedgerStaysLive:
    """LIVENESS pair for the fail-closed reading. A printer where NOTHING is priced
    keeps no spool inventory — an install that never enabled it, a freshly onboarded
    printer, a repair that left every slot unbound. Refusing every roll there is not
    protection, it is a printer that can never print again, so the unknown reserve
    stands down and dispatch proceeds exactly as before."""

    def test_unpriced_printer_still_dispatches(self):
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        out = _match([_req()], loaded, policy="first_loaded", inv={}, min_start_g=150, require_known_grams=True)
        assert out.mapping == [0]
        assert out.start_blocked_slots == []

    def test_unread_only_inventory_is_not_a_speaking_ledger(self):
        """Rows exist but none can quote grams (every tray seated-unidentified) — still
        no ledger to fail closed against; the unread exclusion owns that case."""
        seated_unread = _loaded(0, tray_id=0) | {"unread": True, "type": ""}
        loaded = [seated_unread, _loaded(1, tray_id=1)]
        inv = {0: SlotInventory(remaining_g=None, first_loaded_ord=None, unread=True)}
        out = _match([_req()], loaded, policy="slot_order", inv=inv, min_start_g=150, require_known_grams=True)
        assert out.mapping == [1]  # the other tray still starts the print
        assert out.start_blocked_slots == []

    def test_one_priced_slot_arms_the_reading(self):
        """The threshold is exactly one priced slot: the ledger speaks, so the unpriced
        roll beside it is reserved (the production shape)."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        inv = {1: SlotInventory(remaining_g=800.0, first_loaded_ord=200.0)}
        out = _match([_req()], loaded, policy="first_loaded", inv=inv, min_start_g=150, require_known_grams=True)
        assert out.mapping == [1]  # the priced roll starts, the unpriced one never does
        assert out.start_blocked_slots == []


class TestDominantStartBlock:
    def test_empty_is_no_block(self):
        assert dominant_start_block([]) is None

    def test_all_unknown_reads_unknown(self):
        assert dominant_start_block([START_BLOCK_UNKNOWN_GRAMS, START_BLOCK_UNKNOWN_GRAMS]) == START_BLOCK_UNKNOWN_GRAMS

    def test_any_priced_roll_reads_below_floor(self):
        assert dominant_start_block([START_BLOCK_UNKNOWN_GRAMS, START_BLOCK_BELOW_FLOOR]) == START_BLOCK_BELOW_FLOOR

    def test_outcome_kind_derives_from_its_slots(self):
        """``start_blocked_slots`` and the kind are two views of ONE record, so they
        cannot drift apart."""
        out = MatchOutcome(mapping=None, start_block_kinds={1: START_BLOCK_UNKNOWN_GRAMS, 2: START_BLOCK_BELOW_FLOOR})
        assert out.start_blocked_slots == [1, 2]
        assert out.start_block_kind == START_BLOCK_BELOW_FLOOR


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

    def test_archived_excluded_like_spent(self):
        """An ARCHIVED (retired) row is hard-excluded too. Repairs archive a rotten
        row without necessarily rebinding the slot, and until 2026-08-02 such a row
        stayed a live START candidate."""
        loaded = [_loaded(0, tray_id=0, color="#FF0000")]
        inv = {0: SlotInventory(remaining_g=500.0, first_loaded_ord=None, archived=True)}
        out = _match([_req(color="#FF0000")], loaded, policy="slot_order", inv=inv, min_start_g=0)
        assert out.mapping == [-1]
        assert out.start_blocked_slots == []  # excluded, never merely floor-dropped

    def test_archived_trace_names_its_own_reason(self, caplog):
        """The three hard-exclude reasons stay SEPARATE in the decision trace, so a
        vanished slot is explainable (jam vs run-dry vs retired row)."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1), _loaded(2, tray_id=2), _loaded(3, tray_id=3)]
        inv = {
            0: SlotInventory(remaining_g=500.0, first_loaded_ord=50.0, out_of_rotation=True),
            1: SlotInventory(remaining_g=500.0, first_loaded_ord=100.0, spent=True),
            2: SlotInventory(remaining_g=500.0, first_loaded_ord=150.0, archived=True),
            3: SlotInventory(remaining_g=500.0, first_loaded_ord=200.0),  # live
        }
        with caplog.at_level(logging.INFO, logger="backend.app.services.spool_selection"):
            out = _match([_req()], loaded, policy="first_loaded", inv=inv)
        assert out.mapping == [3]
        trace = "\n".join(r.message for r in caplog.records)
        assert "excluded_oor=[0]" in trace
        assert "excluded_spent=[1]" in trace
        assert "excluded_archived=[2]" in trace


class TestRemainingGramsOrigin:
    """``SlotInventory.remaining_g`` for an internal-inventory slot has ONE origin:
    :attr:`Spool.remaining_g`, where emptiness is DERIVED from ``spent_at`` rather
    than written into the gram ledger (``weight_used`` stays raw so the
    operator-gated un-spend is lossless). The slot-level ``unread`` verdict outranks
    that number — it speaks for the tray's physical contents, which no row-level
    figure can. Rows are REAL (transient) ``Spool`` objects so these pin the model's
    derivation, not a stub's arithmetic."""

    @staticmethod
    def _db(rows):
        """Stub AsyncSession whose .execute().scalars().all() yields ``rows``."""
        scalars = MagicMock()
        scalars.all.return_value = rows
        result = MagicMock()
        result.scalars.return_value = scalars
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        return db

    @staticmethod
    def _internal_mode():
        return patch("backend.app.services.spool_selection._is_spoolman_mode", new=AsyncMock(return_value=False))

    @staticmethod
    def _assignment(spool, *, ams_id=0, tray_id=0):
        return MagicMock(ams_id=ams_id, tray_id=tray_id, spool=spool)

    @staticmethod
    def _spool(*, weight_used, spent_at=None):
        return Spool(
            label_weight=1000,
            weight_used=weight_used,
            loaded_at=None,
            first_loaded_at=None,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            feed_fault_at=None,
            spent_at=spent_at,
            archived_at=None,
        )

    @pytest.mark.asyncio
    async def test_spent_row_reads_zero_though_its_ledger_says_positive(self):
        """A spent row whose ``label_weight - weight_used`` is a healthy 950 g still
        publishes 0.0: the stamp is the hardware's statement that the roll ran dry,
        and an under-counted ledger must never present it as printable material.
        The ``spent`` flag rides separately (it drives the hard exclude)."""
        spool = self._spool(weight_used=50.0, spent_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
        loaded = [_loaded(0, tray_id=0)]
        with self._internal_mode():
            out = await build_slot_inventory(self._db([self._assignment(spool)]), printer_id=1, loaded=loaded)
        assert out[0].remaining_g == 0.0
        assert out[0].spent is True
        # Storage is untouched — the derivation floors nothing on the row itself.
        assert spool.weight_used == 50.0

    @pytest.mark.asyncio
    async def test_unread_slot_outranks_a_spent_row(self):
        """Precedence: a SEATED-but-unidentified tray publishes ``None``
        (undetermined) even when the binding it displaced is spent. The wire says a
        roll is physically there and the farm cannot name it, so neither the ledger
        figure NOR the derived zero is knowledge about this tray."""
        spool = self._spool(weight_used=50.0, spent_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
        loaded = [dict(_loaded(0, tray_id=0), unread=True)]
        with self._internal_mode():
            out = await build_slot_inventory(self._db([self._assignment(spool)]), printer_id=1, loaded=loaded)
        assert out[0].remaining_g is None
        assert out[0].unread is True
        assert out[0].spent is True  # the flag still reaches the trace / hard exclude

    @pytest.mark.asyncio
    async def test_live_row_still_publishes_its_ledger_remaining(self):
        """The derivation is a spent-only override: a live row publishes the plain
        ``label_weight - weight_used`` it always did."""
        loaded = [_loaded(0, tray_id=0)]
        with self._internal_mode():
            out = await build_slot_inventory(
                self._db([self._assignment(self._spool(weight_used=250.0))]), printer_id=1, loaded=loaded
            )
        assert out[0].remaining_g == 750.0
        assert out[0].spent is False

    def test_spent_slot_selection_outcome_unchanged_by_the_zero(self):
        """Selection is untouched by the derived zero: spent slots are hard-excluded
        (``excluded_spent``) BEFORE any remaining-grams read, so a spent slot is never
        a candidate and never a floor-drop — whatever number it publishes."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        for spent_remaining in (0.0, 500.0):
            inv = {
                0: SlotInventory(remaining_g=spent_remaining, first_loaded_ord=50.0, spent=True),
                1: SlotInventory(remaining_g=500.0, first_loaded_ord=100.0),
            }
            out = _match([_req()], loaded, policy="first_loaded", inv=inv, min_start_g=150)
            assert out.mapping == [1]
            assert out.start_blocked_slots == []

    def test_archived_false_default_preserves_behavior(self):
        """archived defaults to False — untouched inventories select as before."""
        inv = SlotInventory(remaining_g=500.0, first_loaded_ord=100.0)
        assert inv.archived is False

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


# ---------------------------------------------------------------------------
# Operator PINS (2026-08-12 contract; 003-H2S external-spool dispatch incident)
#
# ``ams_mapping`` is an INSTRUCTION, not a cached derivation. A pin narrows one
# requirement's candidate set to the named tray INSIDE this matcher — it is never a
# parallel path, so every rule the matcher applies to an auto-match applies to it too.
# The full origin x spool-type x topology matrix is the test scope (operator ruling 11).
# ---------------------------------------------------------------------------
def _external(gtid=254, *, ftype="PETG", color="#00FF00"):
    """The external/virtual tray as ``_build_loaded_filaments`` emits it — present as a
    candidate if and only if that printer's vt_tray declares a filament type."""
    return {
        "type": ftype,
        "color": color,
        "tray_info_idx": "",
        "ams_id": -1,
        "tray_id": 0 if gtid == 254 else 1,
        "global_tray_id": gtid,
        "is_external": True,
        "remain": -1,
    }


class TestPinsNarrowTheCandidateSet:
    def test_no_pin_leaves_auto_matching_unchanged(self):
        """The whole matrix's control case: pins absent => byte-identical behaviour."""
        loaded = [_loaded(0, tray_id=0, color="#00FF00"), _loaded(1, tray_id=1, color="#FF0000")]
        req = [_req(color="#FF0000")]
        assert _match(req, loaded, policy="slot_order").mapping == [1]
        assert _match(req, loaded, policy="slot_order", pins=None).mapping == [1]
        assert _match(req, loaded, policy="slot_order", pins=[-1]).mapping == [1]

    def test_lone_minus_one_is_no_pin_not_an_instruction(self):
        """``-1`` is the format's "no tray for this slot" — it must read as ABSENCE of
        an instruction, never as "pin to nothing" (which would strand every item the
        dialog ever sent a partial mapping for)."""
        out = _match([_req()], [_loaded(0, tray_id=0)], policy="slot_order", pins=[-1])
        assert out.mapping == [0]
        assert out.pin_missing == {}
        assert out.is_total is True

    def test_pin_to_present_ams_tray_is_chosen_over_the_auto_match(self):
        loaded = [_loaded(0, tray_id=0, color="#FF0000"), _loaded(1, tray_id=1, color="#0000FF")]
        # Auto-matching would take gtid 0 (exact colour); the pin takes gtid 1.
        assert _match([_req(color="#FF0000")], loaded, policy="slot_order").mapping == [0]
        out = _match([_req(color="#FF0000")], loaded, policy="slot_order", pins=[1])
        assert out.mapping == [1]
        assert out.is_total is True

    def test_pin_to_absent_tray_leaves_the_slot_unmatched(self):
        """The pinned tray is not loaded at all -> unmatched, recorded as a pin miss
        (NOT a start-block: nothing is short, a named roll is missing)."""
        out = _match([_req()], [_loaded(0, tray_id=0)], policy="slot_order", pins=[7])
        assert out.mapping == [-1]
        assert out.pin_missing == {1: 7}
        assert out.pinned_unavailable_slots == [1]
        assert out.start_blocked_slots == []
        assert out.is_total is False

    def test_pin_ignores_filament_type(self):
        """#1722 precedent: an explicit cross-type pick is a ratified operator override.
        The dialog shows the comparison; the matcher does not veto it."""
        loaded = [_loaded(0, tray_id=0, ftype="PETG", color="#00FF00")]
        assert _match([_req(ftype="PLA")], loaded, policy="slot_order").mapping == [-1]
        assert _match([_req(ftype="PLA")], loaded, policy="slot_order", pins=[0]).mapping == [0]

    def test_pin_still_obeys_the_minimum_start_floor(self):
        """No floor exemption for pins — the roll IS there, it just cannot start, so it
        is a START-BLOCK in the floor's own vocabulary, not a missing tray."""
        inv = {0: SlotInventory(remaining_g=50.0, first_loaded_ord=None)}
        out = _match([_req()], [_loaded(0, tray_id=0)], policy="slot_order", inv=inv, min_start_g=120, pins=[0])
        assert out.mapping == [-1]
        assert out.start_block_kinds == {1: START_BLOCK_BELOW_FLOOR}
        assert out.pin_missing == {}, "the tray is present — this is not a pin miss"
        assert out.is_total is False

    def test_print_anyway_is_the_one_override_lane_for_a_pinned_low_roll(self):
        """``skip_filament_check`` reaches the matcher as ``min_start_g=0`` — the single
        sanctioned way past the floor, for pinned and auto-matched slots alike."""
        inv = {0: SlotInventory(remaining_g=50.0, first_loaded_ord=None)}
        out = _match([_req()], [_loaded(0, tray_id=0)], policy="slot_order", inv=inv, min_start_g=0, pins=[0])
        assert out.mapping == [0]
        assert out.is_total is True

    def test_pin_to_unpriceable_roll_blocks_under_the_start_reading(self):
        """The fail-closed floor reading applies to pins too: a roll the ledger cannot
        price may hold 5 g. A priced roll rides along so the ledger demonstrably speaks."""
        loaded = [_loaded(0, tray_id=0), _loaded(1, tray_id=1)]
        inv = {1: SlotInventory(remaining_g=800.0, first_loaded_ord=None)}  # gtid 0 unpriced
        out = _match(
            [_req()], loaded, policy="slot_order", inv=inv, min_start_g=120, require_known_grams=True, pins=[0]
        )
        assert out.start_block_kinds == {1: START_BLOCK_UNKNOWN_GRAMS}
        assert out.pin_missing == {}

    def test_pin_to_spent_or_jammed_roll_reads_as_unavailable(self):
        """The unusable hard-excludes run before the pin can be honoured — a spent or
        out-of-rotation roll is invisible to selection, so the pin finds nothing."""
        for flag in ("spent", "out_of_rotation"):
            inv = {0: SlotInventory(remaining_g=800.0, first_loaded_ord=None, **{flag: True})}
            out = _match([_req()], [_loaded(0, tray_id=0)], policy="slot_order", inv=inv, pins=[0])
            assert out.pin_missing == {1: 0}, flag
            assert out.is_total is False, flag

    def test_pin_to_unread_tray_reads_as_unavailable(self):
        """A seated-but-unidentified tray cannot be named, so it cannot be pinned to —
        the scheduler's answer to this is "read the slot", not "dispatch anyway"."""
        tray = _loaded(0, tray_id=0)
        tray["type"] = ""
        tray["unread"] = True
        out = _match([_req()], [tray], policy="slot_order", pins=[0])
        assert out.pin_missing == {1: 0}
        assert out.is_total is False

    def test_pin_to_wrong_nozzle_reads_as_unavailable(self):
        """Cross-nozzle assignment fails the print, so the hard nozzle filter outranks
        an instruction that would cross it (dual-nozzle topology)."""
        tray = _loaded(4, tray_id=0)
        tray["extruder_id"] = 1
        req = _req()
        req["nozzle_id"] = 0
        out = _match([req], [tray], policy="slot_order", pins=[4])
        assert out.pin_missing == {1: 4}

    def test_duplicate_pins_give_the_tray_to_the_first_slot_only(self):
        """One tray cannot feed two requirements here — the second slot reports its pin
        unavailable rather than silently double-claiming the roll."""
        reqs = [_req(slot_id=1), _req(slot_id=2)]
        out = _match(reqs, [_loaded(0, tray_id=0)], policy="slot_order", pins=[0, 0])
        assert out.mapping == [0, -1]
        assert out.pin_missing == {2: 0}
        assert out.is_total is False

    def test_pins_are_positional_and_independent_per_slot(self):
        """Position = slot_id - 1; an unpinned slot beside a pinned one still auto-matches."""
        loaded = [_loaded(0, tray_id=0, ftype="PLA"), _loaded(1, tray_id=1, ftype="PETG")]
        reqs = [_req(slot_id=1, ftype="PLA"), _req(slot_id=2, ftype="PETG")]
        out = _match(reqs, loaded, policy="slot_order", pins=[-1, 1])
        assert out.mapping == [0, 1]
        assert out.is_total is True

    def test_short_pin_array_leaves_later_slots_auto_matching(self):
        reqs = [_req(slot_id=1, ftype="PLA"), _req(slot_id=2, ftype="PETG")]
        loaded = [_loaded(0, tray_id=0, ftype="PLA"), _loaded(1, tray_id=1, ftype="PETG")]
        out = _match(reqs, loaded, policy="slot_order", pins=[0])
        assert out.mapping == [0, 1]


class TestPinsToExternalTrays:
    """AMS-vs-external is decided by the matcher from live candidates, never by a
    workflow. The external tray is an ORDINARY candidate — present iff that printer's
    vt_tray declares a type — so a pin to it resolves exactly like a pin to an AMS tray.
    This is the printer-1 shape from the incident.
    """

    def test_pin_to_configured_external_is_chosen(self):
        loaded = [_loaded(0, tray_id=0), _external(254)]
        out = _match([_req()], loaded, policy="slot_order", pins=[254])
        assert out.mapping == [254]
        assert out.is_total is True

    def test_pin_to_unconfigured_external_is_unmatched(self):
        """An empty external holder emits NO candidate — which is precisely the printers
        2-10 shape that dispatched ``[254]`` into a machine holding nothing."""
        out = _match([_req()], [_loaded(0, tray_id=0)], policy="slot_order", pins=[254])
        assert out.mapping == [-1]
        assert out.pin_missing == {1: 254}
        assert out.is_total is False

    def test_dual_nozzle_pin_to_255_needs_the_second_vt_configured(self):
        """On dual-nozzle hardware both 254 and 255 are candidates when configured."""
        one_vt = [_external(254)]
        both_vt = [_external(254), _external(255)]
        assert _match([_req()], one_vt, policy="slot_order", pins=[255]).pin_missing == {1: 255}
        assert _match([_req()], both_vt, policy="slot_order", pins=[255]).mapping == [255]

    def test_mixed_ams_and_external_pins_resolve_independently(self):
        """``[3, 254]``: an AMS tray for one slot, the external holder for the other."""
        loaded = [_loaded(3, tray_id=3, ftype="PLA"), _external(254, ftype="PETG")]
        reqs = [_req(slot_id=1, ftype="PLA"), _req(slot_id=2, ftype="PETG")]
        out = _match(reqs, loaded, policy="slot_order", pins=[3, 254])
        assert out.mapping == [3, 254]
        assert out.is_total is True


class TestTotalOutcomeContract:
    """``is_total`` is the dispatch gate: an item may not start while ANY requirement
    is unresolved, because a partial ``-1`` mapping means "nothing feeds this extruder".
    Derived from the REQUIREMENTS, never by scanning the mapping for ``-1``."""

    def test_sparse_slot_ids_are_not_holes(self):
        """A plate using only filament 2 yields ``[-1, gtid]``. The leading ``-1`` is a
        position nothing asked about — reading it as a hole would park the job forever."""
        out = _match([_req(slot_id=2)], [_loaded(4, tray_id=0)], policy="slot_order")
        assert out.mapping == [-1, 4]
        assert out.unmatched_slots == ()
        assert out.is_total is True

    def test_a_genuine_no_match_is_a_hole(self):
        out = _match([_req(ftype="PLA")], [_loaded(0, tray_id=0, ftype="ABS")], policy="slot_order")
        assert out.unmatched_slots == (1,)
        assert out.is_total is False

    def test_mapping_free_outcome_is_total(self):
        """No requirements => nothing unresolved. The fork's legitimate mapping-free
        dispatch must never be confused with a hole."""
        out = _match([], [_loaded(0, tray_id=0)], policy="slot_order")
        assert out.mapping is None
        assert out.is_total is True

    def test_start_blocked_slot_is_also_unresolved(self):
        """The two records agree by construction — a start-blocked slot fed nothing."""
        inv = {0: SlotInventory(remaining_g=50.0, first_loaded_ord=None)}
        out = _match([_req()], [_loaded(0, tray_id=0)], policy="slot_order", inv=inv, min_start_g=120)
        assert out.start_blocked_slots == [1]
        assert out.unmatched_slots == (1,)
        assert out.is_total is False


class TestParsePins:
    """``parse_pins`` is the ONE origin for turning the stored TEXT column into pins."""

    def test_parses_a_stored_mapping(self):
        assert parse_pins("[0, -1, 254]") == [0, -1, 254]

    def test_absent_or_malformed_reads_as_no_instruction(self):
        for raw in (None, "", "not json", '{"a": 1}'):
            assert parse_pins(raw) is None, raw

    def test_booleans_and_non_ints_degrade_to_no_pin_for_that_slot(self):
        """A malformed entry must never wedge dispatch — it reads as "no instruction"
        for its slot while the well-formed entries beside it still apply."""
        assert parse_pins('[true, "3", 4]') == [-1, -1, 4]


class TestFilamentTypeHelpers:
    """The type-equality compare has ONE implementation (it was inlined three times)."""

    def test_types_match_is_case_insensitive_and_canonical_on_both_sides(self):
        """Case folding is what ``canonical_filament_type`` guarantees on both sides —
        the requirement dict carries the slicer's casing, the candidate the firmware's."""
        assert filament_types_match({"type": "pla"}, {"type": "PLA"}) is True
        assert filament_types_match({"type": "PETG"}, {"type": "petg"}) is True
        assert filament_types_match({"type": "PETG"}, {"type": "PLA"}) is False

    def test_missing_or_empty_type_reads_as_empty_string(self):
        assert filament_type_of({}) == ""
        assert filament_type_of({"type": None}) == ""
        assert filament_type_of({"type": "petg"}) == "PETG"
