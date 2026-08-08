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
    RESOLUTION_REASONS,
    BindingView,
    Decision,
    DecisionKind,
    ResolutionContext,
    SlotState,
    derive_state,
    format_slot_event,
    identity_relation,
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

    def test_a_spent_tagged_row_still_keeps_here(self):
        """The respool tiers (doctrine rule 3) are a CONSUMER the orchestrator runs
        on a KEEP — this table never auto-respools."""
        bound = _view(spool_id=37, is_tagless=False, tag_uid="1C63F1E700000100", spent=True)
        obs = _obs(tag_uid="1C63F1E700000100", remain=100)
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound, identity_candidate=bound))
        assert decision.kind is DecisionKind.KEEP


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
        # fingerprint) → mint the clean 4-dimension default identity.
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

    def test_reclaim_preserves_the_row_of_a_roll_that_came_back(self):
        donor = _view(spool_id=168)
        obs = _obs()
        decision = resolve(obs, derive_state(obs, None), ResolutionContext(last_location_candidate=donor))
        assert decision == Decision(DecisionKind.RECLAIM, spool_id=168, reason="last_location_reclaim")

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

    def test_reclaim_outranks_mint(self):
        obs = _obs()
        ctx = ResolutionContext(last_location_candidate=_view(spool_id=168), tagless_default=TAGLESS_DEFAULT)
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
        """The respool tiers' entry point is unchanged: a push that ASSERTS the bound
        row's identity KEEPs at row 2.1, which is where the orchestrator runs them."""
        tag = "1C63F1E700000100"
        uuid = "8AC9EC0847FD41D0890870319F2E1975"
        bound = _view(spool_id=212, is_tagless=False, tag_uid=tag, tray_uuid=uuid, spent=True)
        obs = _obs(tag_uid=tag, tray_uuid=uuid)
        decision = resolve(
            obs,
            derive_state(obs, bound),
            ResolutionContext(binding=bound, qualified_cycle_pending=True, tagless_default=TAGLESS_DEFAULT),
        )
        assert decision == Decision(DecisionKind.KEEP, spool_id=212, reason="identity_matches_bound")

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

    def test_an_exist_bit_alone_is_not_enough(self):
        """The read arm gates on the TRI-STATE ``present is True`` — the same seated-state
        test ``ams_presence.identify_needed``'s spent-occupied arm applies. A verdict
        emitted on weaker evidence than the need authority accepts is a read that is never
        spent, and a reason that never resolves anything (008-H2C's stuck exist bit is
        exactly that shape)."""
        bound = self._spent()
        obs = observe_tray(4, 2, {"id": 2, "state": 9, "tray_type": ""}, exist_bits=1 << (2 * 4 + 2))
        assert obs.present is None and obs.occupancy_signal is True
        decision = resolve(obs, derive_state(obs, bound), ResolutionContext(binding=bound))
        assert decision == Decision(DecisionKind.KEEP, spool_id=212, reason="spent_latch")

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
