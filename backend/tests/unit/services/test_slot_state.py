"""Derived slot state + the identity-resolution decision table (W1).

The pre-W1 system smeared "what roll is in this tray, and which ledger row is it"
across six modules with different presence/identity/settle rules (main.py phase-1,
main.py phase-2, ``spool_tagless.handle_tagless_slot``'s 7-branch tree,
``maybe_autoconfigure_bare_tray``, ``spool_tag_matcher``, ``ams_presence``). This
suite is the replacement contract: ONE table, every row pinned, plus replay
fixtures for the three 2026-08-01 incidents that had no pins at all.

The three replays (wire shapes captured from PRODUCTION on 2026-08-01):

* **001-H2S T3 sibling tag** — a bound slot presented a tag_uid that disagreed with
  its spool's stored one. The first reading of this ("a chimera: the merge kept a
  DEPARTED roll's tray_uuid beside a NEW roll's tag") was WRONG and is retracted:
  Bambu rolls carry TWO RFID tags, one per flange, sharing ONE ``tray_uuid``, and the
  AMS reads whichever side faces the antenna. Production 2026-08-01 settled it on 4/4
  such slots — the wire ``tray_uuid`` equalled the stored one EXACTLY and the wire
  remaining-% agreed with the ledger every time. So ``tray_uuid`` is the roll's
  identity and ``tag_uid`` is a read of one of its chips; a tag-only disagreement is
  a QUESTION (sibling read vs swap), and minting on it would have created a duplicate
  ledger row for a single roll on all four slots.
* **003-H2S T2 / spool 140** — a tagless row with 932 g used stayed bound to a
  physically EMPTY slot forever (``should_keep_on_empty`` kept every live tagless
  roll), so Inventory showed a location that was not true and the next insert
  inherited the grams.
* **007-H2C spool 194** — 51 bind-moves in 5 m 21 s, one tag presenting on two
  trays across partial pushes, each move rewriting ``loaded_at`` and re-publishing
  ``extrusion_cali_sel``.
"""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from backend.app.services.slot_state import (
    IDENTITY_BACKED_REASONS,
    RESOLUTION_REASONS,
    BindingView,
    Decision,
    DecisionKind,
    ResolutionContext,
    SlotState,
    derive_state,
    format_slot_event,
    identity_relation,
    post_state,
    resolve,
)
from backend.app.services.tray_observation import TrayObservation, observe_ams_push, observe_tray

# --- builders ---------------------------------------------------------------


def _obs(**overrides) -> TrayObservation:
    """A configured, seated, tagless PETG tray unless overridden.

    ``identity_asserted`` / ``config_asserted`` are DERIVED here exactly as
    ``observe_tray`` derives them, so no test can build an observation the wire
    could never produce.
    """
    base = {
        "printer_id": 1,
        "ams_id": 0,
        "tray_id": 0,
        "state": 11,
        "present": True,
        "tag_uid": None,
        "tray_uuid": None,
        "tray_type": "PETG",
        "tray_color": "000000FF",
        "tray_info_idx": "GFG02",
        "tray_sub_brands": "Bambu PETG HF",
        "remain": -1,
        "nozzle_temp_min": 230,
        "nozzle_temp_max": 270,
        "exist_bit": None,
    }
    base.update(overrides)
    base["identity_asserted"] = base["tag_uid"] is not None or base["tray_uuid"] is not None
    base["config_asserted"] = any(
        base[k] is not None for k in ("tray_type", "tray_color", "tray_info_idx", "tray_sub_brands")
    )
    return TrayObservation(**base)


def _view(spool_id=140, **overrides) -> BindingView:
    base = {
        "spool_id": spool_id,
        "is_tagless": True,
        "tag_uid": None,
        "sibling_tag_uid": None,
        "tray_uuid": None,
        "spent": False,
        "archived": False,
        "fingerprint_type": "PETG",
        "fingerprint_color": "000000FF",
        "pre_configured": False,
    }
    base.update(overrides)
    return BindingView(**base)


TAGLESS_DEFAULT = {
    "brand": "Bambu Lab",
    "material": "PETG",
    "subtype": "HF",
    "rgba": "000000FF",
    "slicer_filament": "GFG02",
    "nozzle_temp_min": 230,
    "nozzle_temp_max": 270,
}


# --- derive_state -----------------------------------------------------------


class TestDeriveState:
    def test_spent_binding_latches_regardless_of_presence(self):
        """W1: the latch is the binding + spool.spent_at, not a presence read —
        presence flaps at the runout instant."""
        spent = _view(spent=True)
        for present in (True, False, None):
            assert derive_state(_obs(present=present), spent) is SlotState.SPENT_AWAITING_SWAP

    def test_pre_configured_while_not_present(self):
        pre = _view(pre_configured=True, fingerprint_type=None, fingerprint_color=None)
        for present in (False, None):
            assert derive_state(_obs(present=present, tray_type=""), pre) is SlotState.PRE_CONFIGURED

    def test_pre_configured_stops_applying_once_something_is_present(self):
        pre = _view(pre_configured=True)
        assert derive_state(_obs(present=True), pre) is SlotState.OCCUPIED_ASSUMED

    def test_a_presence_less_dialect_keeps_classifying_pre_configured(self):
        """The PRE_CONFIGURED arm keys on ``present is not True``, so an A1-family/P1S
        push (constant ``state=3`` → presence UNKNOWN) classifies PRE_CONFIGURED even
        though the roll IS seated and configured.

        That stays correct after the finding-A fix, because the state names the DURABLE
        intent while the resolver's row-4 one-shot is what consumes it — and that row
        never reads the state (pinned in ``TestRow4TaglessLane``). Classification and
        apply are deliberately decoupled here; tightening this arm to match the resolver
        would instead delete the "awaiting insert" answer row 3 still needs."""
        pre = _view(pre_configured=True)
        obs = observe_tray(1, 0, {"id": 2, "state": 3, "tray_type": "PETG", "tray_color": "000000FF"})
        assert obs.present is None
        assert derive_state(obs, pre) is SlotState.PRE_CONFIGURED

    @pytest.mark.parametrize(
        ("obs_kwargs", "expected"),
        [
            ({"present": True, "tag_uid": "3CF1F3E700000100"}, SlotState.OCCUPIED_IDENTIFIED),
            ({"present": True, "tray_uuid": "8AC9EC0847FD41D0890870319F2E1975"}, SlotState.OCCUPIED_IDENTIFIED),
            ({"present": True}, SlotState.OCCUPIED_ASSUMED),
            (
                {
                    "present": True,
                    "tray_type": None,
                    "tray_color": None,
                    "tray_info_idx": None,
                    "tray_sub_brands": None,
                },
                SlotState.OCCUPIED_UNRESOLVED,
            ),
            ({"present": True, "tray_type": ""}, SlotState.OCCUPIED_UNRESOLVED),
            ({"present": False, "tray_type": ""}, SlotState.EMPTY),
        ],
    )
    def test_presence_known(self, obs_kwargs, expected):
        assert derive_state(_obs(**obs_kwargs), None) is expected

    @pytest.mark.parametrize(
        ("obs_kwargs", "binding", "expected"),
        [
            # Presence unknown but the push asserted something → classify on that.
            ({"present": None, "tag_uid": "3CF1F3E700000100"}, None, SlotState.OCCUPIED_IDENTIFIED),
            ({"present": None}, None, SlotState.OCCUPIED_ASSUMED),
            # Nothing asserted: any occupancy signal, or a binding, fails toward
            # UNRESOLVED — never toward EMPTY.
            ({"present": None, "tray_type": None, "exist_bit": True}, None, SlotState.OCCUPIED_UNRESOLVED),
            ({"present": None, "tray_type": None, "remain": 40}, None, SlotState.OCCUPIED_UNRESOLVED),
            ({"present": None, "tray_type": None}, _view(), SlotState.OCCUPIED_UNRESOLVED),
            # Only with no binding AND no signal at all is EMPTY the answer.
            ({"present": None, "tray_type": None, "remain": None}, None, SlotState.EMPTY),
        ],
    )
    def test_presence_unknown(self, obs_kwargs, binding, expected):
        kwargs = dict(obs_kwargs)
        if kwargs.get("tray_type", "PETG") is None:
            kwargs.setdefault("tray_color", None)
            kwargs.setdefault("tray_info_idx", None)
            kwargs.setdefault("tray_sub_brands", None)
        assert derive_state(_obs(**kwargs), binding) is expected

    def test_archived_bound_row_on_an_empty_tray_is_empty(self):
        assert derive_state(_obs(present=False, tray_type=""), _view(archived=True)) is SlotState.EMPTY


# --- Row 1: wire safety -----------------------------------------------------


class TestRow1WireSafety:
    @pytest.mark.parametrize(
        ("flag", "reason"),
        [("drying", "ams_drying"), ("identify_in_flight", "identify_in_flight"), ("settling", "insertion_settling")],
    )
    def test_defers(self, flag, reason):
        ctx = ResolutionContext(**{flag: True})
        obs = _obs()
        decision = resolve(obs, derive_state(obs, None), ctx)
        assert decision.kind is DecisionKind.DEFER
        assert decision.reason == reason

    @pytest.mark.parametrize("flag", ["drying", "identify_in_flight", "settling"])
    def test_defer_outranks_a_perfect_tag_match(self, flag):
        """Wire safety is authoritative (invariant 2): deferring costs one push,
        guessing into a slot the firmware is mid-read costs the firmware's answer."""
        obs = _obs(tag_uid="1C63F1E700000100")
        bound = _view(spool_id=37, is_tagless=False, tag_uid="1C63F1E700000100")
        ctx = ResolutionContext(binding=bound, identity_candidate=bound, **{flag: True})
        assert resolve(obs, derive_state(obs, bound), ctx).kind is DecisionKind.DEFER

    @pytest.mark.parametrize("flag", ["drying", "identify_in_flight", "settling"])
    def test_defer_outranks_a_release(self, flag):
        obs = _obs(present=False, tray_type="")
        bound = _view()
        ctx = ResolutionContext(binding=bound, **{flag: True})
        assert resolve(obs, derive_state(obs, bound), ctx).kind is DecisionKind.DEFER


# --- identity_relation: the truth table row 2 is built on -------------------


class TestIdentityRelation:
    """``tray_uuid`` decides; ``tag_uid`` can identify but can never falsify."""

    UUID_A = "8AC9EC0847FD41D0890870319F2E1975"
    UUID_B = "3C78FA47DFCC4F0C8C95566C77A73DCE"
    TAG_A = "EC96F1E700000100"
    TAG_B = "3CF1F3E700000100"

    @pytest.mark.parametrize(
        ("view_kwargs", "obs_kwargs", "expected", "why"),
        [
            # Both uuids asserted → the uuid alone decides, tags irrelevant.
            (
                {"tag_uid": TAG_A, "tray_uuid": UUID_A},
                {"tag_uid": TAG_A, "tray_uuid": UUID_A},
                "same",
                "everything agrees",
            ),
            (
                {"tag_uid": TAG_A, "tray_uuid": UUID_A},
                {"tag_uid": TAG_B, "tray_uuid": UUID_A},
                "same",
                "SIBLING READ: uuid agrees, tag is the other chip",
            ),
            (
                {"tag_uid": TAG_A, "tray_uuid": UUID_A},
                {"tag_uid": TAG_A, "tray_uuid": UUID_B},
                "different",
                "uuids disagree — proof of another roll, even on a shared tag",
            ),
            # No uuid pair → fall back to the tag, which can confirm but not deny.
            ({"tag_uid": TAG_A}, {"tag_uid": TAG_A}, "same", "one chip belongs to one roll"),
            ({"tag_uid": TAG_A}, {"tag_uid": TAG_B}, "ambiguous", "sibling read vs swap — unknowable"),
            (
                {"tag_uid": TAG_A, "tray_uuid": UUID_A},
                {"tag_uid": TAG_B},
                "ambiguous",
                "the push asserted no uuid, so the deciding member is missing",
            ),
            # Nothing comparable.
            ({"tray_uuid": UUID_A}, {"tag_uid": TAG_A}, "ambiguous", "disjoint members"),
            ({}, {"tag_uid": TAG_A, "tray_uuid": UUID_A}, "ambiguous", "tagless row asserts nothing"),
            ({"tag_uid": TAG_A, "tray_uuid": UUID_A}, {}, "ambiguous", "push asserts nothing"),
        ],
    )
    def test_truth_table(self, view_kwargs, obs_kwargs, expected, why):
        assert identity_relation(_view(**view_kwargs), _obs(**obs_kwargs)) == expected, why

    def test_comparison_is_case_insensitive(self):
        """Stored values come from the DB in whatever case they were written; wire
        values are normalized upper. A case difference is not a roll difference."""
        view = _view(tag_uid=self.TAG_A.lower(), tray_uuid=self.UUID_A.lower())
        assert identity_relation(view, _obs(tag_uid=self.TAG_A, tray_uuid=self.UUID_A)) == "same"

    def test_no_verdict_is_ever_none(self):
        """The three verdicts are exhaustive — callers branch on strings, so a fourth
        (or a None) would silently fall through every branch."""
        views = (
            {},
            {"tag_uid": self.TAG_A},
            {"tray_uuid": self.UUID_A},
            {"tag_uid": self.TAG_A, "tray_uuid": self.UUID_A},
        )
        observations = (
            {},
            {"tag_uid": self.TAG_B},
            {"tray_uuid": self.UUID_B},
            {"tag_uid": self.TAG_B, "tray_uuid": self.UUID_B},
        )
        for view_kwargs in views:
            for obs_kwargs in observations:
                assert identity_relation(_view(**view_kwargs), _obs(**obs_kwargs)) in {
                    "same",
                    "different",
                    "ambiguous",
                }


# --- Row 2: the identity lane (uuid-primary) --------------------------------


class TestRow2IdentityLane:
    def test_bound_spool_owns_the_tag_keeps(self):
        bound = _view(spool_id=37, is_tagless=False, tag_uid="1C63F1E700000100")
        obs = _obs(tag_uid="1C63F1E700000100")
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound, identity_candidate=bound))
        assert decision == Decision(DecisionKind.KEEP, spool_id=37, reason="identity_matches_bound")

    def test_identity_resolves_to_a_different_row_binds_it(self):
        """Reaching the candidate lane needs the binding PROVEN different, which only a
        uuid disagreement can do — a tag-only disagreement is ambiguous (pinned below)."""
        bound = _view(
            spool_id=46,
            is_tagless=False,
            tag_uid="EC96F1E700000100",
            tray_uuid="8AC9EC0847FD41D0890870319F2E1975",
        )
        candidate = _view(
            spool_id=99,
            is_tagless=False,
            tag_uid="3CF1F3E700000100",
            tray_uuid="3C78FA47DFCC4F0C8C95566C77A73DCE",
        )
        obs = _obs(tag_uid="3CF1F3E700000100", tray_uuid="3C78FA47DFCC4F0C8C95566C77A73DCE")
        decision = resolve(
            obs, derive_state(obs, bound), ResolutionContext(binding=bound, identity_candidate=candidate)
        )
        assert decision.kind is DecisionKind.BIND
        assert decision.spool_id == 99
        assert decision.reason == "identity_resolved_candidate"

    def test_tag_on_an_unbound_slot_binds(self):
        candidate = _view(spool_id=226, is_tagless=False, tag_uid="EE78F1E700000100")
        obs = _obs(tag_uid="EE78F1E700000100")
        decision = resolve(obs, derive_state(obs, None), ResolutionContext(identity_candidate=candidate))
        assert (decision.kind, decision.spool_id) == (DecisionKind.BIND, 226)
        assert decision.reason == "identity_resolved_candidate"

    def test_a_candidate_resolved_back_to_the_bound_row_keeps(self):
        """The binding view carried no identity of its own, but the caller's uuid-primary
        lookup resolved to that SAME spool — there is nothing to move."""
        bound = _view(spool_id=228, is_tagless=False)
        candidate = _view(
            spool_id=228,
            is_tagless=False,
            tag_uid="EE78F1E700000100",
            tray_uuid="0F8FCF6039964FB68F94A59F8B0897D8",
        )
        obs = _obs(tag_uid="EE78F1E700000100", tray_uuid="0F8FCF6039964FB68F94A59F8B0897D8")
        decision = resolve(
            obs, derive_state(obs, bound), ResolutionContext(binding=bound, identity_candidate=candidate)
        )
        assert decision == Decision(DecisionKind.KEEP, spool_id=228, reason="identity_matches_bound")

    def test_unknown_full_pair_mints_when_auto_add_is_on(self):
        obs = _obs(tag_uid="3CF1F3E700000100", tray_uuid="8AC9EC0847FD41D0890870319F2E1975")
        decision = resolve(obs, derive_state(obs, None), ResolutionContext(auto_add_unknown=True))
        assert decision.kind is DecisionKind.MINT
        assert decision.reason == "unknown_identity_auto_add"
        assert decision.mint_spec["tag_uid"] == "3CF1F3E700000100"
        assert decision.mint_spec["tray_uuid"] == "8AC9EC0847FD41D0890870319F2E1975"
        assert decision.mint_spec["replace_existing"] is False

    def test_unknown_full_pair_over_a_uuid_proven_different_binding_replaces(self):
        bound = _view(
            spool_id=46,
            is_tagless=False,
            tag_uid="EC96F1E700000100",
            tray_uuid="8AC9EC0847FD41D0890870319F2E1975",
        )
        obs = _obs(tag_uid="3CF1F3E700000100", tray_uuid="3C78FA47DFCC4F0C8C95566C77A73DCE")
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound, auto_add_unknown=True))
        assert decision.kind is DecisionKind.MINT
        assert decision.reason == "unknown_identity_auto_add"
        assert decision.mint_spec["replace_existing"] is True

    def test_unknown_full_pair_without_auto_add_owes_a_prompt(self):
        obs = _obs(tag_uid="3CF1F3E700000100", tray_uuid="8AC9EC0847FD41D0890870319F2E1975")
        decision = resolve(obs, derive_state(obs, None), ResolutionContext(auto_add_unknown=False))
        assert decision == Decision(DecisionKind.NONE, reason="unknown_tag_prompt_owed")

    def test_an_rfid_roll_claiming_a_tagless_bound_slot_displaces_it(self):
        """A tagless binding asserts NO identity, so there is nothing to be ambiguous
        ABOUT — it is not a sibling candidate, just an older claim on the slot. Mirrors
        ``spool_tagless``'s provisional disposal when a tag claims an ams_auto row."""
        bound = _view(spool_id=140)  # tagless: tag_uid and tray_uuid both None
        obs = _obs(tag_uid="3CF1F3E700000100", tray_uuid="8AC9EC0847FD41D0890870319F2E1975")
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound, auto_add_unknown=True))
        assert decision.kind is DecisionKind.MINT
        assert decision.mint_spec["replace_existing"] is True

    def test_a_candidate_contradicting_the_wire_is_not_a_match(self):
        """Caller-contract defence: ``identity_candidate`` is documented as an EXACT,
        uuid-primary lookup, but a resolver that widened a match (suffix-LIKE, first-char
        variance) must not be able to smuggle a disagreeing row past the table. This
        candidate is uuid-PROVEN to be another roll, so it is refused as a match."""
        bound = _view(
            spool_id=46,
            is_tagless=False,
            tag_uid="EC96F1E700000100",
            tray_uuid="8AC9EC0847FD41D0890870319F2E1975",
        )
        bogus = _view(
            spool_id=99,
            is_tagless=False,
            tag_uid="3CF1F3E700000100",
            tray_uuid="A74AC09B2B8443BCB0112C15631EFCEC",
        )
        obs = _obs(tag_uid="3CF1F3E700000100", tray_uuid="3C78FA47DFCC4F0C8C95566C77A73DCE")
        decision = resolve(
            obs,
            derive_state(obs, bound),
            ResolutionContext(binding=bound, identity_candidate=bogus, auto_add_unknown=True),
        )
        assert decision.kind is DecisionKind.MINT
        assert decision.reason == "unknown_identity_auto_add"

    def test_uuid_match_keeps_the_binding_across_a_tag_change(self):
        """THE INVERSION (2026-08-01). Bambu rolls carry TWO RFID tags sharing ONE
        ``tray_uuid`` and the AMS reads whichever flange faces its antenna, so a uuid
        agreement KEEPS the binding even though the tag visibly changed. The rule this
        replaces ("uuid alone never keeps a binding whose tag changed") would have minted
        a duplicate row for a roll that never moved — on 4/4 live prod slots."""
        bound = _view(
            spool_id=46,
            is_tagless=False,
            tag_uid="EC96F1E700000100",
            tray_uuid="8AC9EC0847FD41D0890870319F2E1975",
        )
        obs = _obs(tag_uid="3CF1F3E700000100", tray_uuid="8AC9EC0847FD41D0890870319F2E1975")
        decision = resolve(
            obs,
            derive_state(obs, bound),
            ResolutionContext(binding=bound, identity_candidate=bound, auto_add_unknown=True),
        )
        assert decision == Decision(DecisionKind.KEEP, spool_id=46, reason="sibling_tag_read")

    def test_tag_only_disagreement_defers_for_a_full_read(self):
        """Sibling read or genuine swap? This push cannot tell and the two demand
        opposite actions, so the table buys the answer with an identify instead of
        guessing. Never a mint — that is what would duplicate a roll."""
        bound = _view(
            spool_id=46,
            is_tagless=False,
            tag_uid="EC96F1E700000100",
            tray_uuid="8AC9EC0847FD41D0890870319F2E1975",
        )
        obs = _obs(tag_uid="3CF1F3E700000100")
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound, auto_add_unknown=True))
        assert decision == Decision(DecisionKind.DEFER, reason="identity_ambiguous_owed_full_read")

    def test_tag_only_disagreement_defers_even_when_a_row_owns_the_scanned_tag(self):
        """The ambiguity is about the ROLL, not about whether some row owns that tag —
        a legacy twin minted from a B-side read owns exactly such a tag. The full read
        decides, and it arrives carrying the uuid that settles it."""
        bound = _view(
            spool_id=46,
            is_tagless=False,
            tag_uid="EC96F1E700000100",
            tray_uuid="8AC9EC0847FD41D0890870319F2E1975",
        )
        candidate = _view(spool_id=99, is_tagless=False, tag_uid="3CF1F3E700000100")
        obs = _obs(tag_uid="3CF1F3E700000100")
        decision = resolve(
            obs,
            derive_state(obs, bound),
            ResolutionContext(binding=bound, identity_candidate=candidate, auto_add_unknown=True),
        )
        assert decision.kind is DecisionKind.DEFER
        assert decision.reason == "identity_ambiguous_owed_full_read"

    @pytest.mark.parametrize(
        ("obs_kwargs", "label"),
        [
            ({"tag_uid": "3CF1F3E700000100"}, "tag-only"),
            ({"tray_uuid": "8AC9EC0847FD41D0890870319F2E1975"}, "uuid-only"),
        ],
    )
    def test_minting_requires_the_full_identity_pair(self, obs_kwargs, label):
        """Sibling-duplicate protection. A B-side tag read on a partial push looks
        exactly like a brand-new roll, and a uuid-only push names a roll without saying
        which chip was read — neither may mint. The mint lane wants BOTH members stored,
        so the next sibling read of that roll resolves on the uuid."""
        obs = _obs(**obs_kwargs)
        decision = resolve(obs, derive_state(obs, None), ResolutionContext(auto_add_unknown=True))
        assert decision == Decision(DecisionKind.DEFER, reason="partial_identity_owed_full_read"), label

    def test_a_genuine_swap_is_uuid_proven_and_binds_the_new_row(self):
        """The shape the retracted chimera rule was reaching for, done honestly: BOTH
        sides assert a uuid and they DIFFER — positive proof of another roll."""
        bound = _view(
            spool_id=46,
            is_tagless=False,
            tag_uid="EC96F1E700000100",
            tray_uuid="8AC9EC0847FD41D0890870319F2E1975",
        )
        candidate = _view(
            spool_id=186,
            is_tagless=False,
            tag_uid="CBB0D0FE00000100",
            tray_uuid="A74AC09B2B8443BCB0112C15631EFCEC",
        )
        obs = _obs(tag_uid="CBB0D0FE00000100", tray_uuid="A74AC09B2B8443BCB0112C15631EFCEC")
        decision = resolve(
            obs, derive_state(obs, bound), ResolutionContext(binding=bound, identity_candidate=candidate)
        )
        assert decision == Decision(DecisionKind.BIND, spool_id=186, reason="identity_resolved_candidate")

    def test_a_genuine_swap_to_an_unknown_roll_mints(self):
        bound = _view(
            spool_id=46,
            is_tagless=False,
            tag_uid="EC96F1E700000100",
            tray_uuid="8AC9EC0847FD41D0890870319F2E1975",
        )
        obs = _obs(tag_uid="2338393200000100", tray_uuid="A74AC09B2B8443BCB0112C15631EFCEC")
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound, auto_add_unknown=True))
        assert decision.kind is DecisionKind.MINT
        assert decision.reason == "unknown_identity_auto_add"
        assert decision.mint_spec["replace_existing"] is True

    def test_identity_asserted_on_a_cleared_tray_is_a_conflict(self):
        obs = _obs(present=False, tray_type="", tag_uid="3CF1F3E700000100")
        bound = _view()
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision == Decision(DecisionKind.NONE, reason="identity_presence_conflict")

    def test_a_spent_tagged_row_reading_loaded_is_a_new_roll_on_a_reused_core(self):
        """G3 / operator ruling 3 (2026-08-19) — the INVERSION of the old contract.

        This test used to be ``test_a_spent_tagged_row_still_keeps_here`` and pinned the
        opposite: a spent tagged row KEEPing while "the respool tiers are a CONSUMER the
        orchestrator runs on a KEEP". The KEEP was the bug. A runout means the row reached
        zero, and filament cannot be added to a 0 g roll — so the same tag reading back
        over a seated tray is a NEW roll on a reused core, and keeping the drained row
        bound left the fresh roll printing against a ledger reading 0 g remaining. The row
        is retired and its successor minted CARRYING THE TAG, concluded from evidence and
        never asked.
        """
        bound = _view(spool_id=37, is_tagless=False, tag_uid="1C63F1E700000100", spent=True)
        obs = _obs(tag_uid="1C63F1E700000100", remain=100)
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound, identity_candidate=bound))
        assert decision.kind is DecisionKind.REPLACE_SPENT
        assert decision.reason == "reused_core_swap"
        # The drained row is the one being retired…
        assert decision.spool_id == 37
        # …and the successor carries the tag, so the reused core stays resolvable on the
        # next push instead of leaving two active rows claiming one identity.
        assert decision.mint_spec["tag_uid"] == "1C63F1E700000100"
        assert decision.mint_spec["source"] == "tray"

    def test_a_LIVE_tagged_row_reading_loaded_still_keeps_by_identity(self):
        """LIVENESS PAIR for the row above — G3 must not swallow the ordinary case.

        The same shape with ``spent=False`` is a tagged roll simply being re-read, which
        is the overwhelmingly common event on this fleet. It KEEPs, exactly as before.
        """
        bound = _view(spool_id=37, is_tagless=False, tag_uid="1C63F1E700000100", spent=False)
        obs = _obs(tag_uid="1C63F1E700000100", remain=100)
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound, identity_candidate=bound))
        assert decision == Decision(DecisionKind.KEEP, spool_id=37, reason="identity_matches_bound")

    def test_a_spent_tagged_row_whose_presence_is_UNKNOWN_keeps_its_latch(self):
        """"The tray reads loaded" is the tri-state True (invariant 3), not "not empty".

        A push that asserts the tag but says nothing about presence is no evidence a fresh
        roll is seated, and retiring a row on it would be acting on an unknown. Erring LATE
        costs one pushall; erring early destroys a ledger row.
        """
        bound = _view(spool_id=37, is_tagless=False, tag_uid="1C63F1E700000100", spent=True)
        obs = _obs(tag_uid="1C63F1E700000100", present=None)
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision.kind is DecisionKind.KEEP

    def test_an_UNBOUND_spent_row_owning_this_tag_is_never_resurrected(self):
        """G3's second shape, and the branch that had no test in either direction.

        The normal post-runout state for a tagged roll is spent AND UNBOUND: the AMS clears
        the drained slot's exist bit ~3 min BEFORE it declares the runout, so the binding is
        released first and the spent stamp lands on an unbound row (the 2026-08-13 tier-2
        attribution). The operator then puts a fresh roll on the reused core; the tag reads;
        and row 2.3 used to BIND the drained row straight back onto the slot — the fresh
        roll instantly reading 0 g remaining and staging every run behind it. That is
        incident shape 31 reached through the tagged door.

        The finished row is not a bindable owner, so it is discarded as a candidate and the
        ordinary unowned-identity lane resolves the slot: a FRESH row carrying the tag
        (G3 — "mint a fresh row carrying the tag"). The refusal alone would close the
        resurrection and leave the slot unresolved forever, which is the same silence in a
        different costume.
        """
        tag = "1C63F1E700000100"
        uuid = "8AC9EC0847FD41D0890870319F2E1975"
        finished = _view(spool_id=292, is_tagless=False, tag_uid=tag, tray_uuid=uuid, spent=True)
        obs = _obs(tag_uid=tag, tray_uuid=uuid)
        decision = resolve(obs, derive_state(obs, None), ResolutionContext(identity_candidate=finished))
        assert decision.kind is DecisionKind.MINT
        assert decision.reason == "unknown_identity_auto_add"
        # The successor carries BOTH identity members, so the next push resolves on the
        # uuid instead of re-deciding this branch.
        assert decision.mint_spec["tag_uid"] == tag
        assert decision.mint_spec["tray_uuid"] == uuid
        # …and it never names the finished row: nothing binds to it, nothing retires it
        # here (the table mutates nothing — the writer retires the superseded owner).
        assert decision.spool_id is None

    def test_a_finished_owner_is_refused_even_when_presence_is_UNKNOWN(self):
        """The refusal is NOT presence-gated, and the asymmetry is the reason.

        Presence gates the RETIRE in row 2.0, where acting on an unknown would destroy a
        ledger row. Refusing to BIND costs nothing in any presence state, so gating it the
        same way would leave the resurrection door open on exactly the shape that walks
        through it most — an incremental push carrying the tag and no tray state at all.
        A bind not made self-heals on the next push; a resurrected spent row does not.
        """
        tag = "1C63F1E700000100"
        uuid = "8AC9EC0847FD41D0890870319F2E1975"
        finished = _view(spool_id=292, is_tagless=False, tag_uid=tag, tray_uuid=uuid, spent=True)
        obs = _obs(tag_uid=tag, tray_uuid=uuid, present=None)
        decision = resolve(obs, derive_state(obs, None), ResolutionContext(identity_candidate=finished))
        assert decision.spool_id != 292
        assert decision.kind is DecisionKind.MINT

    def test_a_finished_owner_on_a_PARTIAL_read_still_buys_the_full_one(self):
        """Discarding the owner hands the slot to the ordinary lane — gates included.

        A reused core has no exemption from the full-pair rule: a push carrying one member
        of the identity pair is indistinguishable from a B-side sibling read, and minting on
        it is how one roll becomes two rows. Emitting the mint inside row 2.3a would have
        skipped this DEFER, which is the whole argument for discarding the candidate rather
        than writing a second mint there.
        """
        tag = "1C63F1E700000100"
        finished = _view(spool_id=292, is_tagless=False, tag_uid=tag, tray_uuid=None, spent=True)
        obs = _obs(tag_uid=tag)
        assert obs.tray_uuid is None
        decision = resolve(obs, derive_state(obs, None), ResolutionContext(identity_candidate=finished))
        assert decision == Decision(DecisionKind.DEFER, reason="partial_identity_owed_full_read")

    def test_a_finished_owner_with_auto_add_OFF_prompts_instead_of_minting(self):
        """The CONCLUSION is never asked; whether the farm may create rows unattended is.

        Ruling 3 settles "this is a different roll" from evidence. It says nothing about
        ``auto_add_unknown``, which is a separate operator policy over every unknown roll —
        so the discarded candidate lands on the same prompt lane any other unowned identity
        would. What matters is what does NOT happen: no bind onto the finished row.
        """
        tag = "1C63F1E700000100"
        uuid = "8AC9EC0847FD41D0890870319F2E1975"
        finished = _view(spool_id=292, is_tagless=False, tag_uid=tag, tray_uuid=uuid, spent=True)
        obs = _obs(tag_uid=tag, tray_uuid=uuid)
        decision = resolve(
            obs,
            derive_state(obs, None),
            ResolutionContext(identity_candidate=finished, auto_add_unknown=False),
        )
        assert decision == Decision(DecisionKind.NONE, reason="unknown_tag_prompt_owed")

    def test_an_UNBOUND_LIVE_row_owning_this_tag_still_binds(self):
        """LIVENESS PAIR — the other direction of the same untested branch.

        A live row that owns the identity is still the answer (``identity_resolved_candidate``,
        scenario G2 — a tagged roll moved to another printer). Only FINISHED rolls are
        refused, and only because a finished roll is not a live occupant.
        """
        tag = "1C63F1E700000100"
        uuid = "8AC9EC0847FD41D0890870319F2E1975"
        live = _view(spool_id=292, is_tagless=False, tag_uid=tag, tray_uuid=uuid, spent=False)
        obs = _obs(tag_uid=tag, tray_uuid=uuid)
        decision = resolve(obs, derive_state(obs, None), ResolutionContext(identity_candidate=live))
        assert decision == Decision(DecisionKind.BIND, spool_id=292, reason="identity_resolved_candidate")

    def test_a_finished_candidate_never_ROUTES_A_RETIRE_at_a_LIVE_incumbent(self):
        """A finished CANDIDATE never becomes a ``REPLACE_SPENT``, and that is structural.

        ``REPLACE_SPENT`` archives whatever holds the SLOT (``_apply_replace_spent`` takes
        its departed row from the assignment, which is what every emission's
        ``binding.spool_id`` names). A finished candidate is not this slot's occupant, so
        routing it through that arm would retire the live roll standing in the tray. The
        table therefore mints instead: the incumbent is displaced by the writer's ordinary
        move semantics — exactly as a LIVE owner's bind would displace it — and nothing
        retires spool 46.
        """
        tag = "1C63F1E700000100"
        uuid = "8AC9EC0847FD41D0890870319F2E1975"
        incumbent = _view(
            spool_id=46, is_tagless=False, tag_uid="EC96F1E700000100", tray_uuid="A74AC09B2B8443BCB0112C15631EFCEC"
        )
        finished = _view(spool_id=292, is_tagless=False, tag_uid=tag, tray_uuid=uuid, spent=True)
        obs = _obs(tag_uid=tag, tray_uuid=uuid)
        decision = resolve(
            obs,
            derive_state(obs, incumbent),
            ResolutionContext(binding=incumbent, identity_candidate=finished),
        )
        assert decision.kind is DecisionKind.MINT
        assert decision.mint_spec["replace_existing"] is True
        # Neither the finished row nor the incumbent is named for retirement.
        assert decision.spool_id is None


class TestRow2UntaggedClaim:
    """FINDING B — a tagged insert must not displace the weighed row it belongs to.

    Pre-cutover an unknown tag first tried ``spool_tag_matcher.find_matching_untagged_spool``
    and, on a hit, ``link_tag_to_inventory_spool`` moved the identity ONTO that
    operator-created row; a BOUND slot never reached the mint lane at all, so a
    weigh-then-assign pre-config row simply received its roll's tag at insert. After the
    cutover the table minted a fresh ``rfid_auto`` row (default 1000 g label) and
    orphaned the operator's weighed one. ``untagged_claim_candidate`` restores both
    shapes, under the SAME evidence gate as the mint it precedes.
    """

    TAG = "3CF1F3E700000100"
    UUID = "8AC9EC0847FD41D0890870319F2E1975"

    def _tagged(self, **kw):
        return _obs(tag_uid=self.TAG, tray_uuid=self.UUID, **kw)

    def test_the_slots_own_pre_config_row_claims_the_identity(self):
        """The weighed row keeps its slot and takes the tag — no mint, no displacement."""
        pre = _view(spool_id=210, pre_configured=True)
        obs = self._tagged()
        decision = resolve(
            obs,
            derive_state(obs, pre),
            ResolutionContext(binding=pre, untagged_claim_candidate=pre, auto_add_unknown=True),
        )
        assert decision == Decision(DecisionKind.BIND, spool_id=210, reason="pre_configured_apply_identity")

    def test_a_pre_config_binding_actually_reaches_the_claim_branch(self):
        """PINNED, not assumed: the route to 2.4 runs THROUGH 2.2's tagless-livelock
        gate. 2.1 cannot fire (a row asserting no identity is never ``same``), 2.2 defers
        only a binding that DOES claim an identity, and 2.3 finds no owner. If 2.2 ever
        started deferring identity-less bindings, this lane would go silently dead — so
        the fall-through is asserted here rather than inferred."""
        pre = _view(spool_id=210, pre_configured=True)
        obs = self._tagged()
        assert identity_relation(pre, obs) == "ambiguous"  # 2.1 cannot match
        assert not (pre.tag_uid or pre.tray_uuid)  # 2.2's gate lets it through
        # With no claim supplied the same push runs off the end of row 2 into the MINT —
        # proof it was never stopped at 2.2.
        fell_through = resolve(obs, derive_state(obs, pre), ResolutionContext(binding=pre, auto_add_unknown=True))
        assert fell_through.kind is DecisionKind.MINT

    def test_an_unbound_slot_attracts_an_untagged_inventory_row(self):
        candidate = _view(spool_id=300)
        obs = self._tagged()
        decision = resolve(
            obs,
            derive_state(obs, None),
            ResolutionContext(untagged_claim_candidate=candidate, auto_add_unknown=True),
        )
        assert decision == Decision(DecisionKind.BIND, spool_id=300, reason="identity_claims_untagged_row")

    def test_a_claim_that_is_not_the_slots_pre_config_row_is_the_plain_attract(self):
        """Reason discipline: ``pre_configured_apply_identity`` names the slot's OWN
        pre-configured row (a same-spool upsert with a marker to clear); ANY other row is
        the attract lane, where the writer's move semantics take the slot for it."""
        pre = _view(spool_id=210, pre_configured=True)
        other = _view(spool_id=300)
        obs = self._tagged()
        decision = resolve(
            obs,
            derive_state(obs, pre),
            ResolutionContext(binding=pre, untagged_claim_candidate=other, auto_add_unknown=True),
        )
        assert decision == Decision(DecisionKind.BIND, spool_id=300, reason="identity_claims_untagged_row")

    def test_mint_still_fires_with_no_claim_candidate(self):
        """The mint lane is narrowed, not replaced: a genuinely new roll still mints."""
        obs = self._tagged()
        decision = resolve(obs, derive_state(obs, None), ResolutionContext(auto_add_unknown=True))
        assert decision.kind is DecisionKind.MINT
        assert decision.reason == "unknown_identity_auto_add"

    @pytest.mark.parametrize(
        ("obs_kwargs", "label"), [({"tag_uid": TAG}, "tag-only"), ({"tray_uuid": UUID}, "uuid-only")]
    )
    def test_partial_identity_still_defers_even_with_a_claim(self, obs_kwargs, label):
        """The claim branch sits BEHIND the full-pair gate. Landing a tag on an
        operator's row from a half-read carries the same duplicate hazard as minting from
        one, and it would additionally write a wrong identity onto a row the operator
        owns — strictly worse. The evidence gate lives in the TABLE, which is why the
        orchestrator hands the candidate over even on a partial push."""
        candidate = _view(spool_id=300)
        obs = _obs(**obs_kwargs)
        decision = resolve(
            obs,
            derive_state(obs, None),
            ResolutionContext(untagged_claim_candidate=candidate, auto_add_unknown=True),
        )
        assert decision == Decision(DecisionKind.DEFER, reason="partial_identity_owed_full_read"), label

    def test_a_claim_outranks_the_prompt_when_auto_add_is_off(self):
        """A roll the operator already logged is not an unknown roll: nothing to
        auto-add, nothing to ask about."""
        candidate = _view(spool_id=300)
        obs = self._tagged()
        decision = resolve(
            obs,
            derive_state(obs, None),
            ResolutionContext(untagged_claim_candidate=candidate, auto_add_unknown=False),
        )
        assert decision == Decision(DecisionKind.BIND, spool_id=300, reason="identity_claims_untagged_row")

    def test_an_exact_owner_still_outranks_a_claim(self):
        """2.3 is untouched: an EXACT identity owner is certainty, a claim is only a
        plausibility, so the claim may never overtake it."""
        owner = _view(spool_id=99, is_tagless=False, tag_uid=self.TAG, tray_uuid=self.UUID)
        candidate = _view(spool_id=300)
        obs = self._tagged()
        decision = resolve(
            obs,
            derive_state(obs, None),
            ResolutionContext(identity_candidate=owner, untagged_claim_candidate=candidate),
        )
        assert decision == Decision(DecisionKind.BIND, spool_id=99, reason="identity_resolved_candidate")


# --- Row 3: release on empty ------------------------------------------------


class TestRow3ReleaseOnEmpty:
    def test_live_tagless_row_is_released(self):
        bound = _view(spool_id=140)
        obs = _obs(present=False, tray_type="")
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision == Decision(DecisionKind.RELEASE, spool_id=140, reason="cleared_tray")

    def test_spent_row_keeps_the_w1_latch(self):
        bound = _view(spool_id=96, spent=True)
        obs = _obs(present=False, tray_type="")
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision == Decision(DecisionKind.KEEP, spool_id=96, reason="spent_latch_on_empty")

    def test_pre_configured_row_keeps_awaiting_insert(self):
        bound = _view(spool_id=210, pre_configured=True, fingerprint_type=None, fingerprint_color=None)
        obs = _obs(present=False, tray_type="")
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision == Decision(DecisionKind.KEEP, spool_id=210, reason="pre_configured_awaiting_insert")

    def test_archived_row_is_released_like_any_other(self):
        bound = _view(spool_id=55, archived=True)
        obs = _obs(present=False, tray_type="")
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision.kind is DecisionKind.RELEASE

    def test_unbound_empty_slot_is_a_no_op(self):
        obs = _obs(present=False, tray_type="")
        assert resolve(obs, derive_state(obs, None), ResolutionContext()) == Decision(
            DecisionKind.NONE, reason="empty_unbound"
        )


# --- Row 4: the tagless lane ------------------------------------------------


class TestRow4TaglessLane:
    def test_spent_latch_without_a_cycle_keeps(self):
        bound = _view(spool_id=95, spent=True)
        obs = _obs()
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision == Decision(DecisionKind.KEEP, spool_id=95, reason="spent_latch")

    def test_qualified_cycle_releases_the_latch_into_a_silent_replace(self):
        bound = _view(spool_id=95, spent=True)
        obs = _obs()
        decision = resolve(
            obs,
            derive_state(obs, bound),
            ResolutionContext(binding=bound, qualified_cycle_pending=True, tagless_default=TAGLESS_DEFAULT),
        )
        assert decision.kind is DecisionKind.REPLACE_SPENT
        assert decision.spool_id == 95
        assert decision.reason == "spent_swap_confirmed"
        # Firmware leftover config (the tray still reads the DEPARTED roll's
        # fingerprint) → mint the fleet default's complete identity.
        assert decision.mint_spec["source"] == "tagless_default"
        assert decision.mint_spec["default_filament"] == TAGLESS_DEFAULT

    def test_replace_spent_mints_from_the_tray_when_the_filament_genuinely_differs(self):
        bound = _view(spool_id=95, spent=True, fingerprint_type="PETG", fingerprint_color="000000FF")
        obs = _obs(tray_type="PLA", tray_color="FF0000FF")
        decision = resolve(
            obs,
            derive_state(obs, bound),
            ResolutionContext(binding=bound, qualified_cycle_pending=True, tagless_default=TAGLESS_DEFAULT),
        )
        assert decision.mint_spec["source"] == "tray"
        assert decision.mint_spec["tray_type"] == "PLA"

    def test_pre_config_one_shot_applies_on_insert(self):
        bound = _view(spool_id=210, pre_configured=True)
        obs = _obs(present=True)
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision == Decision(DecisionKind.BIND, spool_id=210, reason="pre_configured_apply")

    @pytest.mark.parametrize(
        ("tray", "label"),
        [
            ({"id": 2, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"}, "a presence-reporting dialect"),
            ({"id": 2, "state": 3, "tray_type": "PETG", "tray_color": "000000FF"}, "A1-family/P1S constant state=3"),
            ({"id": 2, "tray_type": "PETG", "tray_color": "000000FF"}, "a push carrying no state at all"),
        ],
    )
    def test_pre_config_one_shot_applies_on_every_dialect(self, tray, label):
        """FINDING A. The A1-family and P1S firmwares report a CONSTANT ``state=3`` (and
        some pushes omit ``state`` entirely), so ``tray_fields.tray_presence`` can only ever answer
        UNKNOWN for them. Gating the one-shot on ``present is True`` therefore left those
        printers "awaiting insert" FOREVER — the roll seated, configured, and never
        applied (upstream #1322, which the pre-cutover replay handled). The gate is
        ``present is not False``, and within row 4 that is never False."""
        bound = _view(spool_id=210, pre_configured=True)
        obs = observe_tray(1, 0, tray)
        assert obs.present is not False, label
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision == Decision(DecisionKind.BIND, spool_id=210, reason="pre_configured_apply"), label

    def test_a_debounce_preserves_the_row_when_the_absence_was_short_and_uncaused(self):
        """T1 — a glitched exist bit, the roll never moved. The lane's ONE real job."""
        donor = _view(spool_id=168)
        obs = _obs()
        ctx = ResolutionContext(debounce_candidate=donor, reseat_within_window=True)
        decision = resolve(obs, derive_state(obs, None), ctx)
        assert decision == Decision(DecisionKind.RECLAIM, spool_id=168, reason="reseat_debounce")

    def test_a_donor_outside_the_window_mints_instead(self):
        """T2/T3/T5 — shape 32. Outside the window the farm asserts NOTHING."""
        obs = _obs()
        ctx = ResolutionContext(
            debounce_candidate=_view(spool_id=292), reseat_within_window=False, tagless_default=TAGLESS_DEFAULT
        )
        decision = resolve(obs, derive_state(obs, None), ctx)
        assert decision.kind is DecisionKind.MINT
        assert decision.reason == "tagless_mint"

    def test_a_runout_suspect_slot_mints_even_inside_the_window(self):
        """T7/T8 — operator ruling 15: a runout release is never a glitch.

        The regression gate for this wave. Without it, scoping the lane to short gaps
        CONCENTRATES the reclaim on exactly the refill-after-runout case, because a refill
        on a demanded slot is precisely a fast return.
        """
        obs = _obs()
        ctx = ResolutionContext(
            debounce_candidate=_view(spool_id=292),
            reseat_within_window=True,
            runout_suspect=True,
            tagless_default=TAGLESS_DEFAULT,
        )
        decision = resolve(obs, derive_state(obs, None), ctx)
        assert decision.kind is DecisionKind.MINT
        assert decision.reason == "runout_suspect_mint"

    def test_cause_is_weighed_before_duration(self):
        """Doctrine rule 6 applied to a decision: BY CAUSE, never by timing.

        Pinned as its own case because the two orderings are indistinguishable on the
        happy path and only diverge here.
        """
        obs = _obs()
        both = ResolutionContext(
            debounce_candidate=_view(spool_id=1),
            reseat_within_window=True,
            runout_suspect=True,
            tagless_default=TAGLESS_DEFAULT,
        )
        assert resolve(obs, derive_state(obs, None), both).kind is DecisionKind.MINT

    def test_mint_when_there_is_nothing_to_reclaim(self):
        obs = _obs()
        decision = resolve(obs, derive_state(obs, None), ResolutionContext(tagless_default=TAGLESS_DEFAULT))
        assert decision.kind is DecisionKind.MINT
        assert decision.reason == "tagless_mint"
        # A CONFIGURED tray mints its own identity — the default is for bare trays
        # and for the post-runout leftover-config case.
        assert decision.mint_spec["source"] == "tray"
        assert decision.mint_spec["tray_type"] == "PETG"
        assert decision.mint_spec["replace_existing"] is False

    def test_debounce_outranks_mint_inside_the_window(self):
        obs = _obs()
        ctx = ResolutionContext(
            debounce_candidate=_view(spool_id=168), reseat_within_window=True, tagless_default=TAGLESS_DEFAULT
        )
        assert resolve(obs, derive_state(obs, None), ctx).kind is DecisionKind.RECLAIM

    @pytest.mark.parametrize(
        ("tray_color", "fp_color", "tray_type", "fp_type"),
        [
            ("000000FF", "000000FF", "PETG", "PETG"),
            ("000000FF", "000000FF", "petg", "PETG"),  # canonicalised type
            ("7CC4D5FF", "56B7E6FF", "PETG", "PETG"),  # RFID read variance, within tolerance
            ("000000FF", "000000FF", "PA-CF", "PA12-CF"),  # firmware-equivalent group
        ],
    )
    def test_matching_fingerprint_keeps(self, tray_color, fp_color, tray_type, fp_type):
        bound = _view(spool_id=228, fingerprint_type=fp_type, fingerprint_color=fp_color)
        obs = _obs(tray_type=tray_type, tray_color=tray_color)
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision == Decision(DecisionKind.KEEP, spool_id=228, reason="fingerprint_matches")

    @pytest.mark.parametrize(
        ("tray_color", "tray_type"),
        [("FF0000FF", "PETG"), ("000000FF", "PLA"), ("FF0000FF", "ABS")],
    )
    def test_different_filament_mints_over_the_bound_row(self, tray_color, tray_type):
        bound = _view(spool_id=228)
        obs = _obs(tray_type=tray_type, tray_color=tray_color)
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision.kind is DecisionKind.MINT
        assert decision.reason == "different_filament"
        assert decision.mint_spec["replace_existing"] is True

    def test_archived_bound_row_never_keeps_a_present_tray(self):
        """Row 6: an archived row is not a live occupant, even on a perfect
        fingerprint match — it must not hold a location claim."""
        bound = _view(spool_id=55, archived=True)
        obs = _obs()
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision.kind is DecisionKind.MINT
        assert decision.reason == "archived_bound_replaced"
        assert decision.mint_spec["replace_existing"] is True

    def test_a_tagged_row_is_never_re_decided_by_a_push_without_rfid_fields(self):
        """Periodic AMS pushes routinely omit tag_uid/tray_uuid — that is why the
        merge preserves them. A tagged binding must not be minted over just because
        THIS push carried config and no identity (the pre-cutover tagless branch tree's
        (2) "not ours" return, whose owner ``handle_tagless_slot`` the W3 cutover
        deleted)."""
        bound = _view(spool_id=37, is_tagless=False, tag_uid="1C63F1E700000100", fingerprint_type="PLA")
        obs = _obs(tray_type="PETG", tray_color="FF0000FF")
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision == Decision(DecisionKind.KEEP, spool_id=37, reason="tagged_row_awaits_tag_lane")

    def test_a_spent_TAGGED_row_is_replaced_on_a_qualified_cycle_too(self):
        """2026-08-07 (printer 4 tray 2). Row 4a used to require ``is_tagless``, on the
        reasoning that a spent TAGGED row belongs to the respool tiers (doctrine rule 3).
        That was the deadlock: the tiers act when a TAG IS READ — row 2 — and row 4 is
        reached precisely when no tag was read, so a spent tagged row met NEITHER lane and
        latched its slot against the roll physically seated in it (spool 212, 1121.5 g on
        a 1000 g label, a ~90 %-full replacement unread beneath it for a day). Spent-ness
        is decided by the runout, not by who minted the row."""
        bound = _view(spool_id=212, is_tagless=False, tag_uid="1C63F1E700000100", spent=True)
        obs = _obs()
        decision = resolve(
            obs,
            derive_state(obs, bound),
            ResolutionContext(binding=bound, qualified_cycle_pending=True, tagless_default=TAGLESS_DEFAULT),
        )
        assert decision.kind is DecisionKind.REPLACE_SPENT
        assert decision.spool_id == 212
        assert decision.reason == "spent_swap_confirmed"
        assert decision.mint_spec["source"] == "tagless_default"

    def test_a_spent_TAGGED_row_still_latches_without_a_cycle(self):
        """Rule 3 is untouched and the latch is not weakened: with no qualified physical
        cycle the spent tagged row KEEPs, exactly like the tagless one — the runout-instant
        flap must not phantom-mint over a still-present dead roll."""
        bound = _view(spool_id=212, is_tagless=False, tag_uid="1C63F1E700000100", spent=True)
        obs = _obs()
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision == Decision(DecisionKind.KEEP, spool_id=212, reason="spent_latch")

    def test_a_tag_read_over_a_spent_row_still_belongs_to_row_2(self):
        """Row ownership is unchanged: a push that ASSERTS the bound row's identity is
        decided by the IDENTITY lane, never by row 4's tagless machinery.

        What row 2 concludes there changed on 2026-08-19 (G3 — the tag is on a reused
        core, so the drained row retires and its successor carries the tag), and the
        distinction this test exists for is unaffected: the identity lane owns the push.
        The proof is in the mint spec — row 2 mints from the TRAY's identity, while row
        4a's ``spent_swap_confirmed`` would have minted from the tagless default and
        thrown the tag away.
        """
        tag = "1C63F1E700000100"
        uuid = "8AC9EC0847FD41D0890870319F2E1975"
        bound = _view(spool_id=212, is_tagless=False, tag_uid=tag, tray_uuid=uuid, spent=True)
        obs = _obs(tag_uid=tag, tray_uuid=uuid)
        decision = resolve(
            obs,
            derive_state(obs, bound),
            ResolutionContext(binding=bound, qualified_cycle_pending=True, tagless_default=TAGLESS_DEFAULT),
        )
        assert decision.reason == "reused_core_swap"
        assert decision.spool_id == 212
        assert decision.mint_spec["source"] == "tray"
        assert decision.mint_spec["tray_uuid"] == uuid

    def test_tagless_lane_runs_mid_print(self):
        """A configured tagless slot is OCCUPIED_ASSUMED, not UNRESOLVED — the
        mid-print no-touch rule (doctrine rule 5) is about UNKNOWN identity."""
        bound = _view(spool_id=228)
        obs = _obs()
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound, busy=True))
        assert decision.kind is DecisionKind.KEEP


# --- Rows 5/6: unresolved identity ------------------------------------------


class TestRow5Unresolved:
    def _bare(self, **kw):
        return _obs(tray_type=None, tray_color=None, tray_info_idx=None, tray_sub_brands=None, **kw)

    def test_busy_defers(self):
        obs = self._bare()
        decision = resolve(obs, derive_state(obs, None), ResolutionContext(busy=True))
        assert decision == Decision(DecisionKind.DEFER, reason="mid_print_unresolved")

    def test_idle_owes_an_identify_but_mutates_nothing(self):
        obs = self._bare()
        decision = resolve(obs, derive_state(obs, None), ResolutionContext(busy=False))
        assert decision == Decision(DecisionKind.NONE, reason="identity_unresolved")

    def test_busy_defer_applies_to_a_bound_slot_too(self):
        bound = _view(spool_id=228)
        obs = self._bare()
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound, busy=True))
        assert decision.kind is DecisionKind.DEFER

    def test_spent_latch_survives_a_signal_less_push(self):
        bound = _view(spool_id=95, spent=True)
        obs = self._bare(present=None)
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound, busy=True))
        assert decision == Decision(DecisionKind.KEEP, spool_id=95, reason="spent_latch")

    def test_pre_configured_survives_a_signal_less_push(self):
        bound = _view(spool_id=210, pre_configured=True)
        obs = self._bare(present=None)
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision == Decision(DecisionKind.KEEP, spool_id=210, reason="pre_configured_awaiting_insert")

    def test_nothing_at_all(self):
        obs = self._bare(present=None, state=None, remain=None)
        decision = resolve(obs, derive_state(obs, None), ResolutionContext())
        assert decision == Decision(DecisionKind.NONE, reason="empty_unbound")


class TestSpentOccupiedOwesAnIdentify:
    """2026-08-07, printer 4 tray 2 — the spent latch that locked out its replacement.

    A spent ``rfid_auto`` binding (spool 212, 1121.5 g used on a 1000 g label) sat over a
    tray reading state 10 with a ~90 %-full roll in it that the wire had never named. The
    unconditional rows-5/6 spent KEEP meant the slot owed NOTHING — no identify was ever
    scheduled, so the newcomer's tag was never read, so the slot never left the latch. It
    stood a full day with zero farm reaction.

    A latch may explain a slot that is EMPTY (row 3's exemption — the core left) or a
    slot that said NOTHING this push (presence unknown). It may not explain a PRESENT
    tray forever: something is seated there, and the only thing that can name it is a
    read. Nothing is mutated here — the verdict buys an answer, exactly like row 5's
    unresolved arm."""

    PRESENT_UNNAMED = {"id": 2, "state": 10}  # the prod shape: seated, no config, no tag

    def _spent(self, **kw):
        return _view(spool_id=212, spent=True, **kw)

    @pytest.mark.parametrize(
        ("view_kwargs", "label"),
        [
            ({}, "a tagless spent row"),
            ({"is_tagless": False, "tag_uid": "1C63F1E700000100"}, "the prod rfid_auto spent row"),
        ],
    )
    def test_idle_owes_a_discovery_read(self, view_kwargs, label):
        bound = self._spent(**view_kwargs)
        obs = observe_tray(4, 0, dict(self.PRESENT_UNNAMED))
        assert (obs.present, obs.identity_asserted, obs.config_nonempty) == (True, False, False), label
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision == Decision(DecisionKind.NONE, reason="spent_occupied_owed_identify"), label

    def test_busy_defers_instead(self):
        """Doctrine rule 5: mid-print inserts are never auto-read — the same answer the
        unresolved arm gives, so the mid-print no-touch rule keeps ONE meaning."""
        bound = self._spent()
        obs = observe_tray(4, 0, dict(self.PRESENT_UNNAMED))
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound, busy=True))
        assert decision == Decision(DecisionKind.DEFER, reason="mid_print_unresolved")

    def test_presence_unknown_still_latches(self):
        """A push that says nothing about the slot is not evidence a roll is seated: the
        latch is the durable state and an unknown is never resolved toward action."""
        bound = self._spent()
        obs = _obs(present=None, state=None, tray_type=None, tray_color=None, tray_info_idx=None, tray_sub_brands=None)
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision == Decision(DecisionKind.KEEP, spool_id=212, reason="spent_latch")

    def test_a_trusted_exist_bit_IS_presence_and_earns_the_read(self):
        """The read arm gates on the TRI-STATE ``present is True``, which a trusted exist
        bit now answers on its own — and ``ams_presence.identify_needed``'s spent-occupied
        arm grants a read on exactly that. The two must agree: a verdict emitted on
        evidence the need authority does not accept is a read that is never spent, and a
        reason that never resolves anything."""
        bound = self._spent()
        obs = observe_tray(4, 2, {"id": 2, "state": 9, "tray_type": ""}, exist_bits=1 << (2 * 4 + 2))
        assert obs.present is True and obs.occupancy_signal is True
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision == Decision(DecisionKind.NONE, reason="spent_occupied_owed_identify")

    def test_an_empty_tray_keeps_the_row_3_latch(self):
        """The boundary is unmoved: presence FALSE never reaches rows 5/6 — row 3 owns it,
        and its spent exemption (the core physically left, the latch survives) stands."""
        bound = self._spent()
        obs = observe_tray(4, 0, {"id": 2, "state": 9, "tray_type": ""})
        assert obs.present is False
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision == Decision(DecisionKind.KEEP, spool_id=212, reason="spent_latch_on_empty")


class TestSpentSwapOnANoTagRead:
    """2026-08-07, spool 226 / 001-H2S slot 1 — the answer that concluded nothing.

    A spent RFID-TAGGED binding sat on a slot into which the operator had inserted a fresh
    TAGLESS roll. The spent-occupied arm above earned the slot a discovery read, and that
    read answered NO TAG (the expected answer for every tagless roll) — but the arm only ever
    handled the tag-FOUND outcome, which lands on row 2. The no-tag outcome concluded nothing:
    the tray was bare, so row 4's qualified-cycle machinery was unreachable by construction,
    and the slot re-owed a read it had already been given, forever.

    A commanded read that found no chip over a binding that HAS one is positive proof of a
    different roll — the same certainty class as a ``tray_uuid`` disagreement — so it
    resolves here."""

    PRESENT_UNNAMED = {"id": 1, "state": 10}  # the prod shape: seated, no config, no tag

    def _tagged_spent(self, **kw):
        return _view(spool_id=226, is_tagless=False, tag_uid="1C63F1E700000100", spent=True, **kw)

    def _ctx(self, bound, **kw):
        base = {
            "binding": bound,
            "no_tag_read_answered": True,
            "tagless_default": TAGLESS_DEFAULT,
        }
        base.update(kw)
        return ResolutionContext(**base)

    def _resolve(self, bound, **kw):
        obs = observe_tray(1, 0, dict(self.PRESENT_UNNAMED))
        assert (obs.present, obs.identity_asserted, obs.config_nonempty) == (True, False, False)
        return resolve(obs, derive_state(obs, bound), self._ctx(bound, **kw))

    def test_the_no_tag_answer_swaps_the_spent_tagged_row(self):
        bound = self._tagged_spent()
        decision = self._resolve(bound)
        assert decision.kind is DecisionKind.REPLACE_SPENT
        assert decision.spool_id == 226
        assert decision.reason == "spent_swap_no_tag_read"
        # A bare tray has no fields to mint from, so the replacement comes from the
        # configured tagless default — the same spec builder every default mint uses.
        assert decision.mint_spec["source"] == "tagless_default"
        assert decision.mint_spec["default_filament"] == TAGLESS_DEFAULT

    def test_a_spent_TAGLESS_binding_is_deliberately_excluded(self):
        """Over a spent TAGLESS row a no-tag read proves NOTHING: the same core reads the
        same way before and after a swap (same-core ambiguity). That case keeps belonging to
        the qualified-cycle machinery, which measures a PHYSICAL event instead."""
        bound = _view(spool_id=226, spent=True)  # is_tagless=True
        decision = self._resolve(bound)
        assert decision == Decision(DecisionKind.NONE, reason="spent_occupied_owed_identify")

    def test_without_the_evidence_the_slot_still_only_owes_a_read(self):
        bound = self._tagged_spent()
        decision = self._resolve(bound, no_tag_read_answered=False)
        assert decision == Decision(DecisionKind.NONE, reason="spent_occupied_owed_identify")

    def test_mid_print_still_defers(self):
        """Doctrine rule 5 outranks the evidence: a mid-print insert is never auto-read and
        never auto-swapped, so the answer is unchanged from today's."""
        bound = self._tagged_spent()
        decision = self._resolve(bound, busy=True)
        assert decision == Decision(DecisionKind.DEFER, reason="mid_print_unresolved")

    def test_no_tagless_default_configured_changes_nothing(self):
        """With no default there is nothing to mint the replacement FROM, so the slot keeps
        owing its read rather than binding a row with no identity at all."""
        bound = self._tagged_spent()
        decision = self._resolve(bound, tagless_default=None)
        assert decision == Decision(DecisionKind.NONE, reason="spent_occupied_owed_identify")

    def test_presence_unknown_still_latches_even_with_the_evidence(self):
        """The row lives inside the ``present is True`` arm: a push that said nothing about
        the slot is not evidence a roll is seated, and an unknown is never resolved toward a
        binding write."""
        bound = self._tagged_spent()
        obs = _obs(present=None, state=None, tray_type=None, tray_color=None, tray_info_idx=None, tray_sub_brands=None)
        decision = resolve(obs, derive_state(obs, bound), self._ctx(bound))
        assert decision == Decision(DecisionKind.KEEP, spool_id=226, reason="spent_latch")

    def test_the_reason_is_part_of_the_public_contract(self):
        assert "spent_swap_no_tag_read" in RESOLUTION_REASONS


class TestTheContradictionIsGeneral:
    """Doctrine rule 11 — a tagless roll can NEVER be an RFID roll, in EVERY scenario.

    Operator-ratified 2026-08-19: *"finding no chip over a binding that has one is positive
    proof of a different roll. This should be happening for all scenarios… a tagless cannot
    be an rfid roll in ALL scenarios, im not sure why it's bounded to just spent spools."*

    The table already argued exactly this, in row 5a's own comment ("the same certainty class
    as a uuid disagreement"), and then confined the conclusion to ONE constellation — a spent
    row under a BARE tray. Two gates did the confining and both are gone: ``not
    binding.spent`` in ``slot_pipeline._no_tag_read_answered``, and the strict ``tray_bare``
    it handed ``ams_presence.read_answered_no_tag``. The second was the larger one: a
    third-party PETG roll reports ``tray_type: "PETG"``, ``tray_info_idx: "GFG02"``,
    ``tag_uid: null`` — CONFIGURED, not bare — so the evidence could never fire for the
    commonest physical swap on this fleet.

    The four quadrants of the contradiction, all pinned here:

    * **G7** tagged binding + answered no-tag + a CONFIGURED tray ⇒ retire, mint tagless;
    * **G8** tagless binding + a tag asserted ⇒ displaced by identity (already correct);
    * **G9** tagless binding + answered no-tag ⇒ NO conclusion (the arm that must not fire);
    * **G10** a push that merely OMITS the RFID fields ⇒ KEEP (silence is not an answer).
    """

    LIVE_TAG = "1C63F1E700000200"
    BARE = {"id": 1, "state": 10}  # seated, no config, no tag

    def _tagged_live(self, **kw):
        """The G7 departed row: an RFID Bambu roll, still live, bound to the slot."""
        return _view(spool_id=301, is_tagless=False, tag_uid=self.LIVE_TAG, **kw)

    def _ctx(self, bound, **kw):
        base = {"binding": bound, "no_tag_read_answered": True, "tagless_default": TAGLESS_DEFAULT}
        base.update(kw)
        return ResolutionContext(**base)

    def _resolve(self, obs, bound, **kw):
        return resolve(obs, derive_state(obs, bound), self._ctx(bound, **kw))

    # --- G7: the configured tray, which is the whole point ------------------

    def test_G7_a_CONFIGURED_tray_answering_no_tag_retires_the_tagged_binding(self):
        """THE fix. The tray is configured — asserted here, because a bare-tray-only
        predicate could never fire for it and that is precisely why G7 persisted silently
        with the wrong row bound."""
        obs = _obs()
        assert obs.config_nonempty is True and obs.identity_asserted is False
        bound = self._tagged_live()

        decision = self._resolve(obs, bound)

        assert decision.kind is DecisionKind.MINT
        assert decision.reason == "tagged_swap_no_tag_read"

    def test_G7_the_departed_row_is_UNLINKED_not_archived(self):
        """A roll that LEFT is not a roll that ran dry: it keeps its grams and its history
        and is merely unbound (``replace_existing``), which is what separates this from row
        5a's ``REPLACE_SPENT``. The Decision carries no ``spool_id`` for exactly that reason
        — there is no row to archive."""
        decision = self._resolve(_obs(), self._tagged_live())

        assert decision.kind is DecisionKind.MINT
        assert decision.spool_id is None
        assert decision.mint_spec["replace_existing"] is True

    def test_G7_firmware_leftover_config_mints_from_the_tagless_DEFAULT(self):
        """The configuration a swapped-into tray reports is usually the DEPARTED roll's,
        left behind by the firmware. ``departed=binding`` is what makes the fresh roll get a
        clean identity instead of inheriting the description of the roll that left."""
        decision = self._resolve(_obs(), self._tagged_live())

        assert decision.mint_spec["source"] == "tagless_default"
        assert decision.mint_spec["default_filament"] == TAGLESS_DEFAULT

    def test_G7_a_genuinely_different_filament_mints_from_the_TRAY(self):
        """When the tray's configuration is NOT the departed row's, it is the new roll's own
        and the mint uses it."""
        obs = _obs(tray_type="PLA", tray_color="FF0000FF", tray_info_idx="GFA00")
        decision = self._resolve(obs, self._tagged_live())

        assert decision.reason == "tagged_swap_no_tag_read"
        assert decision.mint_spec["source"] == "tray"
        assert (decision.mint_spec["tray_type"], decision.mint_spec["tray_color"]) == ("PLA", "FF0000FF")

    def test_G7_mid_print_keeps_the_binding(self):
        """Doctrine rule 5 outranks the evidence: mid-print the farm commands no reads, so an
        answer still standing during a print predates it and says nothing about a roll
        inserted since (the scenario-R4 mis-mint this refusal exists to prevent)."""
        decision = self._resolve(_obs(), self._tagged_live(), busy=True)

        assert decision == Decision(DecisionKind.KEEP, spool_id=301, reason="tagged_row_awaits_tag_lane")

    def test_G7_presence_unknown_keeps_the_binding(self):
        """Invariant 3's tri-state: nothing asserted seated, nothing to be a different roll.
        An unknown is never resolved toward a binding write."""
        decision = self._resolve(_obs(present=None, state=None), self._tagged_live())

        assert decision == Decision(DecisionKind.KEEP, spool_id=301, reason="tagged_row_awaits_tag_lane")

    def test_G7_an_operators_pre_config_intent_is_never_guessed_over(self):
        """Scenario T13. A pre-configured binding is the operator saying which roll is going
        into this slot; the one-shot apply outranks the contradiction, exactly as it outranks
        de-bounce and mint — and exactly as ``_operator_recheck_answered`` excludes it."""
        decision = self._resolve(_obs(), self._tagged_live(pre_configured=True))

        assert decision == Decision(DecisionKind.BIND, spool_id=301, reason="pre_configured_apply")

    # --- G10: silence is not an answer --------------------------------------

    def test_G10_a_push_that_merely_OMITS_the_rfid_fields_still_keeps_the_binding(self):
        """Row 4b′'s original contract, which the new arm must not swallow. Periodic AMS
        pushes routinely omit identity — that is why the merge preserves it — so "not asked"
        and "asked and answered" must stay different things."""
        decision = self._resolve(_obs(), self._tagged_live(), no_tag_read_answered=False)

        assert decision == Decision(DecisionKind.KEEP, spool_id=301, reason="tagged_row_awaits_tag_lane")

    def test_G10_holds_for_a_BARE_tray_too(self):
        """The same distinction one row down: with no answer, a bare tray under a live tagged
        binding still only OWES a read."""
        obs = observe_tray(1, 0, dict(self.BARE))
        decision = self._resolve(obs, self._tagged_live(), no_tag_read_answered=False)

        assert decision == Decision(DecisionKind.NONE, reason="identity_unresolved")

    # --- G9: the arm that must NOT fire -------------------------------------

    def test_G9_a_TAGLESS_binding_has_no_claim_to_contradict_configured_tray(self):
        """**Do not "fix" this test.** Rule 11 is one-way BY LOGIC, not by timidity: a
        binding that claims NO identity has nothing for a no-tag read to disagree with, and
        the same bare core reads identically before and after a swap (same-core ambiguity).
        The answer here is the ordinary fingerprint KEEP — the contradiction lane is not
        consulted at all."""
        bound = _view(spool_id=140)  # is_tagless=True
        decision = self._resolve(_obs(), bound)

        assert decision == Decision(DecisionKind.KEEP, spool_id=140, reason="fingerprint_matches")

    def test_G9_a_TAGLESS_binding_has_no_claim_to_contradict_bare_tray(self):
        """**Do not "fix" this test either.** Same logic, bare tray: the slot owes a read,
        and nothing about the no-tag answer licenses retiring a claim-less row. Same-core
        ambiguity is resolved by a PHYSICAL event (the qualified-cycle machinery), never by
        this evidence."""
        obs = observe_tray(1, 0, dict(self.BARE))
        decision = self._resolve(obs, _view(spool_id=140))

        assert decision == Decision(DecisionKind.NONE, reason="identity_unresolved")

    def test_G9_holds_when_the_tagless_row_carries_only_a_SIBLING_tag(self):
        """G6's converse. ``is_tagless`` is the canonical THREE-column test resolved when the
        view was built, so a row whose only identity is the roll's far-side chip is TAGGED —
        it has a claim, and the contradiction applies to it."""
        bound = _view(spool_id=302, is_tagless=False, sibling_tag_uid=self.LIVE_TAG)
        decision = self._resolve(_obs(), bound)

        assert decision.reason == "tagged_swap_no_tag_read"

    # --- G8: tagless → tagged, already correct ------------------------------

    def test_G8_a_tag_asserted_over_a_tagless_binding_binds_the_tags_OWNER(self):
        """Pre-existing behaviour, pinned by its first test — row 2.2 deliberately falls
        through for a claim-less binding ("it simply has no claim to weigh"), and 2.3
        displaces it by identity. Cited, not rebuilt."""
        bound = _view(spool_id=140)  # tagless, holding the slot
        owner = _view(spool_id=303, is_tagless=False, tag_uid=self.LIVE_TAG, tray_uuid="UUID-G8")
        obs = _obs(tag_uid=self.LIVE_TAG, tray_uuid="UUID-G8")

        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound, identity_candidate=owner))

        assert decision == Decision(DecisionKind.BIND, spool_id=303, reason="identity_resolved_candidate")

    def test_G8_an_unknown_tag_over_a_tagless_binding_mints_and_displaces_it(self):
        """The other half of the same row: no row owns the identity, so the full pair mints a
        new RFID row and the tagless row is unlinked (``replace_existing``) — it keeps its
        grams, it simply no longer claims this slot."""
        bound = _view(spool_id=140)
        obs = _obs(tag_uid=self.LIVE_TAG, tray_uuid="UUID-G8")

        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))

        assert decision.kind is DecisionKind.MINT
        assert decision.reason == "unknown_identity_auto_add"
        assert decision.mint_spec["replace_existing"] is True

    # --- the two mirror arms the generalisation also unblocks ---------------

    def test_a_SPENT_tagged_binding_under_a_CONFIGURED_tray_now_concludes_too(self):
        """Row 4a′ — row 5a's missing twin. The same slot, the same answer, parked forever on
        ``spent_latch`` for no better reason than that the firmware had left the departed
        roll's ``tray_type`` behind. Proof does not become less positive because the tray
        happens to carry configuration."""
        bound = self._tagged_live(spent=True)
        decision = self._resolve(_obs(), bound, qualified_cycle_pending=False)

        assert decision.kind is DecisionKind.REPLACE_SPENT
        assert decision.spool_id == 301
        assert decision.reason == "spent_swap_no_tag_read"

    def test_a_measured_physical_cycle_still_wins_its_own_reason(self):
        """Ordering pin: the cycle arm stays FIRST. A measured physical event is the primary
        evidence and keeps ``spent_swap_confirmed``, so the COUNT of each reason in the logs
        goes on meaning what it meant."""
        bound = self._tagged_live(spent=True)
        decision = self._resolve(_obs(), bound, qualified_cycle_pending=True)

        assert decision.reason == "spent_swap_confirmed"

    def test_a_LIVE_tagged_binding_under_a_BARE_tray_mirrors_row_5a(self):
        """Row 5b. Without it this constellation fell to ``identity_unresolved`` and re-owed a
        read it had already been given — the exact silence that parked spool 226 for a day
        (2026-08-07), reproduced one binding-state over. It differs from 5a only in the
        disposition of the departed row: unlinked, not archived."""
        obs = observe_tray(1, 0, dict(self.BARE))
        assert (obs.present, obs.identity_asserted, obs.config_nonempty) == (True, False, False)

        decision = self._resolve(obs, self._tagged_live())

        assert decision.kind is DecisionKind.MINT
        assert decision.reason == "tagged_swap_no_tag_read"
        assert decision.mint_spec["source"] == "tagless_default"
        assert decision.mint_spec["replace_existing"] is True

    def test_a_bare_slot_with_no_tagless_default_keeps_owing_its_read(self):
        """A bare tray carries no fields to mint the replacement FROM, so with no default
        configured there is nothing to bind — the same local guard row 5a has, for the same
        reason. Row 4b′ needs none: a configured tray can always mint from itself."""
        obs = observe_tray(1, 0, dict(self.BARE))
        decision = self._resolve(obs, self._tagged_live(), tagless_default=None)

        assert decision == Decision(DecisionKind.NONE, reason="identity_unresolved")

    def test_a_bare_slot_awaiting_a_PRE_CONFIGURED_roll_is_left_alone(self):
        """Scenario T13 again, one row down: 5b is ordered AFTER the pre-configured KEEP, so
        operator intent is never guessed over here either."""
        obs = observe_tray(1, 0, dict(self.BARE))
        decision = self._resolve(obs, self._tagged_live(pre_configured=True))

        assert decision == Decision(DecisionKind.KEEP, spool_id=301, reason="pre_configured_awaiting_insert")

    def test_the_reason_is_part_of_the_public_contract(self):
        assert "tagged_swap_no_tag_read" in RESOLUTION_REASONS


# --- I/O-free contract ------------------------------------------------------


class TestPurity:
    def test_module_imports_nothing_that_pulls_a_session(self):
        import backend.app.services.slot_state as mod

        source = Path(mod.__file__).read_text(encoding="utf-8")
        for forbidden in ("sqlalchemy", "AsyncSession", "async def", "await ", "models.spool"):
            assert forbidden not in source, f"slot_state must stay I/O-free: found {forbidden!r}"

    def test_every_emitted_reason_is_enumerated(self):
        """``RESOLUTION_REASONS`` is the W3 orchestrator's contract — it branches on
        these strings (sibling KEEPs are INFO-logged, the ``*_owed_full_read`` defers
        drive the idle-identify lane, ``unknown_tag_prompt_owed`` raises the durable
        operator prompt). A reason the table can emit but the set does not name is a
        branch that silently does nothing downstream, so the two are pinned together."""
        import re

        import backend.app.services.slot_state as mod

        source = Path(mod.__file__).read_text(encoding="utf-8")
        # Literals passed as reason=..., plus the two _keep_reason returns.
        emitted = set(re.findall(r'reason="([a-z_]+)"', source)) | {"identity_matches_bound", "sibling_tag_read"}
        assert emitted - RESOLUTION_REASONS == set(), "emitted but not enumerated"
        assert RESOLUTION_REASONS - emitted == set(), "enumerated but no longer emitted"

    def test_decisions_are_frozen(self):
        with pytest.raises(FrozenInstanceError):
            Decision(DecisionKind.KEEP).kind = DecisionKind.MINT  # type: ignore[misc]

    def test_resolve_is_deterministic(self):
        obs = _obs()
        bound = _view(spool_id=228)
        ctx = ResolutionContext(binding=bound)
        assert resolve(obs, derive_state(obs, bound), ctx) == resolve(obs, derive_state(obs, bound), ctx)


# --- replay pins: the three 2026-08-01 incidents ----------------------------


class TestSiblingTagReplay001T3:
    """001-H2S T3, replayed with the TRUE semantics (the chimera reading is retracted).

    Stage 1 — a partial push carries the far-side tag 3CF1 and NO ``tray_uuid`` key,
    while spool 46 stores tag EC96 + uuid 8AC9. Two physical stories fit that push
    equally well (the roll re-seated onto its other flange; the roll swapped for a new
    one), so the table DEFERS and owes the slot an identify.

    Stage 2 — the identify's full read carries BOTH members: tag 3CF1 + uuid 8AC9. The
    uuid settles it — same roll, read on its second chip — and the binding is KEPT.

    That is what production showed on 2026-08-01: on all four slots whose wire tag
    disagreed with the stored one, the wire ``tray_uuid`` was identical to the stored
    one AND the wire remaining-% agreed with the ledger (spool 46 20 % ↔ 180 g, 194
    14 % ↔ 140 g, 196 100 % ↔ 1000 g, 186 100 % ↔ 1000 g). Minting on stage 1 would
    have duplicated all four rolls.
    """

    PUSH_TAG_ONLY = {"id": 3, "tag_uid": "3CF1F3E700000100", "tray_type": "PETG", "state": 11}
    PUSH_FULL_READ = {
        "id": 3,
        "tag_uid": "3CF1F3E700000100",
        "tray_uuid": "8AC9EC0847FD41D0890870319F2E1975",
        "tray_type": "PETG",
        "state": 11,
    }
    PUSH_CLEARED = {"id": 3, "state": 9, "tray_type": ""}

    SPOOL_46 = BindingView(
        spool_id=46,
        is_tagless=False,
        tag_uid="EC96F1E700000100",
        sibling_tag_uid=None,  # the far chip has not been recorded yet — this IS its first sighting
        tray_uuid="8AC9EC0847FD41D0890870319F2E1975",
        spent=False,
        archived=False,
        fingerprint_type="PETG",
        fingerprint_color="000000FF",
        pre_configured=False,
    )

    def test_the_partial_push_asserts_no_uuid(self):
        """The atomic-pair rule is untouched by this correction and is exactly what
        makes the deferral honest: the push asserts only what it carried."""
        obs = observe_tray(1, 0, self.PUSH_TAG_ONLY)
        assert obs.tag_uid == "3CF1F3E700000100"
        assert obs.tray_uuid is None  # never inherited from an earlier push

    def test_stage_1_tag_only_defers_for_a_full_read(self):
        obs = observe_tray(1, 0, self.PUSH_TAG_ONLY)
        decision = resolve(
            obs,
            derive_state(obs, self.SPOOL_46),
            ResolutionContext(binding=self.SPOOL_46, identity_candidate=None, auto_add_unknown=True),
        )
        assert decision == Decision(DecisionKind.DEFER, reason="identity_ambiguous_owed_full_read")

    def test_stage_2_full_read_keeps_the_same_roll(self):
        obs = observe_tray(1, 0, self.PUSH_FULL_READ)
        decision = resolve(
            obs,
            derive_state(obs, self.SPOOL_46),
            ResolutionContext(binding=self.SPOOL_46, identity_candidate=self.SPOOL_46, auto_add_unknown=True),
        )
        assert decision == Decision(DecisionKind.KEEP, spool_id=46, reason="sibling_tag_read")

    def test_no_row_is_minted_anywhere_in_the_sequence(self):
        """The whole point of the correction: one roll, one ledger row."""
        for push in (self.PUSH_TAG_ONLY, self.PUSH_FULL_READ):
            obs = observe_tray(1, 0, push)
            decision = resolve(
                obs,
                derive_state(obs, self.SPOOL_46),
                ResolutionContext(binding=self.SPOOL_46, auto_add_unknown=True),
            )
            assert decision.kind is not DecisionKind.MINT, push

    def test_an_actually_cleared_tray_still_releases(self):
        """Location truth is unchanged by the identity correction — a sibling read is
        not a reason to hold a binding on a slot the wire reports EMPTY."""
        obs = observe_tray(1, 0, self.PUSH_CLEARED)
        decision = resolve(obs, derive_state(obs, self.SPOOL_46), ResolutionContext(binding=self.SPOOL_46))
        assert decision.kind is DecisionKind.RELEASE

    # The four production slots, 2026-08-01. Stored tag ≠ wire tag on every one, and
    # the tray_uuid identical on every one. (The tags abbreviated in the incident note
    # are written out with the constant Bambu family suffix "00000100" that every full
    # value in that capture carries; only the DISAGREEMENT is load-bearing here.)
    PROD_SIBLING_SLOTS = [
        (46, "EC96F1E700000100", "3CF1F3E700000100", "8AC9EC0847FD41D0890870319F2E1975"),
        (194, "A5E7210D00000100", "95F6F50C00000100", "3C78FA47DFCC4F0C8C95566C77A73DCE"),
        (196, "66839BE000000100", "D6385CEC00000100", "0F8FCF6039964FB68F94A59F8B0897D8"),
        (186, "CBB0D0FE00000100", "2338393200000100", "A74AC09B2B8443BCB0112C15631EFCEC"),
    ]

    @pytest.mark.parametrize(("spool_id", "stored_tag", "wire_tag", "tray_uuid"), PROD_SIBLING_SLOTS)
    def test_every_prod_sibling_slot_keeps_its_binding(self, spool_id, stored_tag, wire_tag, tray_uuid):
        bound = _view(spool_id=spool_id, is_tagless=False, tag_uid=stored_tag, tray_uuid=tray_uuid)
        obs = _obs(tag_uid=wire_tag, tray_uuid=tray_uuid)
        decision = resolve(
            obs,
            derive_state(obs, bound),
            ResolutionContext(binding=bound, identity_candidate=bound, auto_add_unknown=True),
        )
        assert decision == Decision(DecisionKind.KEEP, spool_id=spool_id, reason="sibling_tag_read")

    @pytest.mark.parametrize(("spool_id", "stored_tag", "wire_tag", "tray_uuid"), PROD_SIBLING_SLOTS)
    def test_every_prod_sibling_slot_defers_on_the_tag_only_push(self, spool_id, stored_tag, wire_tag, tray_uuid):
        """The same four slots seen through a partial push: DEFER, never MINT."""
        bound = _view(spool_id=spool_id, is_tagless=False, tag_uid=stored_tag, tray_uuid=tray_uuid)
        obs = _obs(tag_uid=wire_tag)
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound, auto_add_unknown=True))
        assert decision == Decision(DecisionKind.DEFER, reason="identity_ambiguous_owed_full_read")


class TestStaleEmptyReplay003T2:
    """003-H2S T2 / spool 140 (932 g used, tagless, live): bound to a slot the wire
    reports EMPTY. ``should_keep_on_empty`` kept it forever; the table releases."""

    TRAY = {"id": 2, "state": 9, "tray_type": ""}
    SPOOL_140 = BindingView(
        spool_id=140,
        is_tagless=True,
        tag_uid=None,
        sibling_tag_uid=None,
        tray_uuid=None,
        spent=False,
        archived=False,
        fingerprint_type="PETG",
        fingerprint_color="000000FF",
        pre_configured=False,
    )

    def test_releases(self):
        obs = observe_tray(3, 0, self.TRAY)
        state = derive_state(obs, self.SPOOL_140)
        assert state is SlotState.EMPTY
        decision = resolve(obs, state, ResolutionContext(binding=self.SPOOL_140))
        assert decision == Decision(DecisionKind.RELEASE, spool_id=140, reason="cleared_tray")

    def test_the_audit_line_is_the_forensic_record(self):
        obs = observe_tray(3, 0, self.TRAY)
        decision = resolve(obs, SlotState.EMPTY, ResolutionContext(binding=self.SPOOL_140))
        line = format_slot_event(3, 0, 2, SlotState.OCCUPIED_ASSUMED, SlotState.EMPTY, decision)
        assert line == "[slot-state] printer=3 A0T2 OCCUPIED_ASSUMED→EMPTY release spool=140 reason=cleared_tray"


class TestFlipFlopReplay007C194:
    """007-H2C spool 194: 51 bind-moves in 5 m 21 s, the same tag presenting on
    AMS1 T0 and T1 across partial pushes (sticky identity + tray-list truncation).

    ``resolve`` is per-slot and PURE, so both trays legitimately answer BIND — that
    is the correct local answer for each. Suppressing the storm is NOT this
    function's job:

    * per-pass dedup (one decision per spool per callback) — orchestrator, W3;
    * the move damper (``RetryWindow(10.0)`` keyed ``spool.id`` alone, MOVES only)
      and the ``extrusion_cali_sel`` throttle — binding writer, W2.

    The cutover integration test asserts ONE bind per pass end-to-end; this pin
    fixes the decision SHAPES those layers will be fed.
    """

    PUSH = {
        "ams": [
            {
                "id": 1,
                "tray": [
                    {"id": 0, "tag_uid": "95F6F1E700000100", "tray_type": "PETG", "state": 11},
                    {"id": 1, "tag_uid": "95F6F1E700000100", "tray_type": "PETG", "state": 11},
                ],
            }
        ]
    }
    SPOOL_194 = BindingView(
        spool_id=194,
        is_tagless=False,
        tag_uid="95F6F1E700000100",
        sibling_tag_uid=None,
        tray_uuid=None,
        spent=False,
        archived=False,
        fingerprint_type="PETG",
        fingerprint_color="000000FF",
        pre_configured=False,
    )

    def test_both_trays_are_observed_independently(self):
        obs = observe_ams_push(7, self.PUSH)
        assert [(o.ams_id, o.tray_id, o.tag_uid) for o in obs] == [
            (1, 0, "95F6F1E700000100"),
            (1, 1, "95F6F1E700000100"),
        ]

    def test_both_slots_resolve_to_bind_the_same_spool(self):
        obs = observe_ams_push(7, self.PUSH)
        decisions = [
            resolve(o, derive_state(o, None), ResolutionContext(identity_candidate=self.SPOOL_194)) for o in obs
        ]
        assert all(d.kind is DecisionKind.BIND for d in decisions)
        assert {d.spool_id for d in decisions} == {194}
        assert {d.reason for d in decisions} == {"identity_resolved_candidate"}

    def test_the_incumbent_tray_keeps_and_only_the_other_binds(self):
        """Once ONE of the two is bound, its own slot answers KEEP — the churn that
        produced 51 moves came from re-deciding both slots from committed reads
        mid-loop, not from the table disagreeing with itself."""
        obs = observe_ams_push(7, self.PUSH)
        held = resolve(
            obs[0],
            derive_state(obs[0], self.SPOOL_194),
            ResolutionContext(binding=self.SPOOL_194, identity_candidate=self.SPOOL_194),
        )
        other = resolve(obs[1], derive_state(obs[1], None), ResolutionContext(identity_candidate=self.SPOOL_194))
        assert held.kind is DecisionKind.KEEP
        assert other.kind is DecisionKind.BIND


# --- post-application state -------------------------------------------------


class TestPostState:
    """``post_state`` is the RIGHT side of the audit line.

    ``derive_state`` is computed against the PRE-transition binding, so using it as the
    right side printed tautologies in production — ``SPENT_AWAITING_SWAP→
    SPENT_AWAITING_SWAP replace_spent`` on the very line an operator reads to see what
    changed.
    """

    @pytest.mark.parametrize("kind", list(DecisionKind))
    def test_every_decision_kind_is_mapped(self, kind):
        """Totality pin: a NEW kind with no mapping raises here instead of silently
        reviving the tautology."""
        assert isinstance(post_state(Decision(kind, reason="x"), SlotState.OCCUPIED_UNRESOLVED), SlotState)

    def test_an_unmapped_kind_fails_loudly(self):
        with pytest.raises(ValueError, match="no mapping"):
            post_state(Decision("not_a_kind"), SlotState.EMPTY)  # type: ignore[arg-type]

    def test_release_lands_empty(self):
        decision = Decision(DecisionKind.RELEASE, spool_id=140, reason="cleared_tray")
        assert post_state(decision, SlotState.OCCUPIED_ASSUMED) is SlotState.EMPTY

    @pytest.mark.parametrize("reason", sorted(IDENTITY_BACKED_REASONS))
    def test_an_identity_backed_decision_lands_identified(self, reason):
        kind = DecisionKind.MINT if reason == "unknown_identity_auto_add" else DecisionKind.BIND
        assert post_state(Decision(kind, spool_id=1, reason=reason), SlotState.EMPTY) is SlotState.OCCUPIED_IDENTIFIED

    @pytest.mark.parametrize(
        ("kind", "reason"),
        [
            # The tagless lane: a fingerprint, an operator's intent or a last-location
            # guess — good evidence, but nothing READ the roll in this slot.
            (DecisionKind.BIND, "pre_configured_apply"),
            (DecisionKind.MINT, "tagless_mint"),
            (DecisionKind.MINT, "different_filament"),
            (DecisionKind.MINT, "archived_bound_replaced"),
            (DecisionKind.RECLAIM, "last_location_reclaim"),
            (DecisionKind.REPLACE_SPENT, "spent_swap_confirmed"),
            (DecisionKind.REPLACE_SPENT, "spent_swap_no_tag_read"),
        ],
    )
    def test_an_assumption_tier_decision_lands_assumed(self, kind, reason):
        assert post_state(Decision(kind, spool_id=1, reason=reason), SlotState.EMPTY) is SlotState.OCCUPIED_ASSUMED

    @pytest.mark.parametrize("kind", [DecisionKind.KEEP, DecisionKind.DEFER, DecisionKind.NONE])
    @pytest.mark.parametrize("derived", list(SlotState))
    def test_a_non_writing_decision_returns_the_derived_state(self, kind, derived):
        assert post_state(Decision(kind, reason="x"), derived) is derived

    def test_the_production_tautology_is_gone(self):
        """The exact prod line: a spent latch replaced by a fresh roll now reads as a
        transition, not as a state repeating itself."""
        decision = Decision(DecisionKind.REPLACE_SPENT, spool_id=212, reason="spent_swap_confirmed")
        derived = SlotState.SPENT_AWAITING_SWAP
        assert post_state(decision, derived) is not derived
        assert post_state(decision, derived) is SlotState.OCCUPIED_ASSUMED


class TestIdentityBackedReasons:
    def test_they_are_all_declared_resolution_reasons(self):
        """A renamed reason must break here, not degrade a slot to ASSUMED in silence."""
        assert IDENTITY_BACKED_REASONS <= RESOLUTION_REASONS

    def test_the_identity_mint_is_exactly_the_one_carrying_a_read_pair(self):
        """The reason set and the mint spec are two views of ONE fact ("the wire
        asserted an identity"); this pins them together so they cannot drift."""
        tagged = _obs(tag_uid="3CF1F3E700000100", tray_uuid="8AC9EC0847FD41D0890870319F2E1975")
        identity_mint = resolve(tagged, derive_state(tagged, None), ResolutionContext(auto_add_unknown=True))
        bare = _obs()
        tagless_mint = resolve(bare, derive_state(bare, None), ResolutionContext())

        assert identity_mint.kind is tagless_mint.kind is DecisionKind.MINT
        assert identity_mint.reason in IDENTITY_BACKED_REASONS
        assert identity_mint.mint_spec["tag_uid"] and identity_mint.mint_spec["tray_uuid"]
        assert tagless_mint.reason not in IDENTITY_BACKED_REASONS
        assert not tagless_mint.mint_spec.get("tag_uid")
        assert not tagless_mint.mint_spec.get("tray_uuid")


# --- log grammar ------------------------------------------------------------


class TestFormatSlotEvent:
    def test_grammar(self):
        decision = Decision(DecisionKind.BIND, spool_id=226, reason="identity_resolved_candidate")
        line = format_slot_event(1, 0, 3, SlotState.EMPTY, SlotState.OCCUPIED_IDENTIFIED, decision)
        assert line == (
            "[slot-state] printer=1 A0T3 EMPTY→OCCUPIED_IDENTIFIED bind spool=226 reason=identity_resolved_candidate"
        )

    def test_missing_spool_and_reason_render_as_dashes(self):
        line = format_slot_event(2, 1, 0, SlotState.EMPTY, SlotState.EMPTY, Decision(DecisionKind.NONE))
        assert line == "[slot-state] printer=2 A1T0 EMPTY→EMPTY none spool=- reason=-"

    def test_states_render_as_their_values_not_repr(self):
        line = format_slot_event(1, 0, 0, SlotState.SPENT_AWAITING_SWAP, SlotState.EMPTY, Decision(DecisionKind.KEEP))
        assert "SlotState." not in line
        assert "SPENT_AWAITING_SWAP→EMPTY keep" in line

    @pytest.mark.parametrize("kind", list(DecisionKind))
    def test_every_kind_renders(self, kind):
        line = format_slot_event(1, 0, 0, SlotState.EMPTY, SlotState.EMPTY, Decision(kind, reason="x"))
        assert f" {kind.value} " in line
