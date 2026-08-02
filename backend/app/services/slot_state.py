"""Derived AMS slot state + the ONE identity-resolution decision table (W1).

Two pure functions and their vocabulary:

* :func:`derive_state` — ``state = f(observation, binding)``. NOTHING is persisted:
  a stored copy would be a second source of truth that can drift from the wire,
  which is the disease this rebuild cures (plan §"Schema — NO NEW TABLES"). The
  durable facts the state is derived FROM already exist: the assignment row,
  ``spool.spent_at`` (the W1 runout latch), ``spool_assignment.pre_configured_at``.
* :func:`resolve` — the decision table. Flat, exhaustively commented, one row per
  situation. It returns a :class:`Decision`; it never performs it.

**I/O-free by contract.** No session, no DB models, no awaits, and no imports of
modules that pull either. The orchestrator (W2/W3) fetches candidates, calls these,
and applies the returned decision. If this module ever needs an ``await``, the
design is wrong.

**Identity model: ``tray_uuid`` is the spool, ``tag_uid`` is one chip of it.**
A Bambu roll carries TWO RFID tags, one per flange, sharing a single ``tray_uuid``;
the AMS reads whichever side faces its antenna. Verified live on production
2026-08-01 across 4/4 disagreeing slots: in every one the wire ``tray_uuid`` matched
the bound spool's stored uuid exactly and the wire remaining-% agreed with the ledger
(spools 46/194/196/186). The fork's own ``spool_respool.RespoolSiblingConflict`` was
written for the same shape. Three consequences drive :func:`identity_relation`:

* a ``tray_uuid`` agreement is proof of the SAME roll even when the tags differ —
  that is a sibling read, and keeping the binding is correct;
* a ``tray_uuid`` disagreement, both sides asserting, is proof of a DIFFERENT roll;
* a tag-only disagreement proves NOTHING (sibling read vs genuine swap are
  indistinguishable without the uuid), so it must defer for a full read rather than
  mint — minting there would create a duplicate ledger row for one physical roll.

The observation layer's atomic-pair rule STAYS exactly as it was: a push asserts only
the members it actually carried, never members inherited from an earlier push. That
rule is precisely what makes :func:`identity_relation` honest — it is the reason
"both sides asserted this member" is a fact and not a merge artefact.

Every ``Decision.reason`` this module can emit is enumerated in
:data:`RESOLUTION_REASONS`; the orchestrator branches on those strings, so the set is
a public contract — add to it deliberately, never silently.

Doctrine rules cited by number below are the operator-ratified spool/AMS doctrine
(skill ``bambu-ams-behavior``); the numbering is a public API — never renumber.

Audit trail: there is no events table (operator ruling). The forensic record is the
structured log line produced by :func:`format_slot_event`, emitted by the
orchestrator through the existing log pipeline / ``support/logs`` endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from backend.app.services.tray_observation import TrayObservation
from backend.app.utils.color_utils import colors_similar
from backend.app.utils.filament_types import canonical_filament_type


class SlotState(str, Enum):
    """Durable-by-derivation slot states (never stored — see the module docstring)."""

    EMPTY = "EMPTY"
    OCCUPIED_IDENTIFIED = "OCCUPIED_IDENTIFIED"
    OCCUPIED_ASSUMED = "OCCUPIED_ASSUMED"
    OCCUPIED_UNRESOLVED = "OCCUPIED_UNRESOLVED"
    SPENT_AWAITING_SWAP = "SPENT_AWAITING_SWAP"
    PRE_CONFIGURED = "PRE_CONFIGURED"


class DecisionKind(str, Enum):
    """What the orchestrator should DO with this slot.

    * ``KEEP`` — the binding is correct; touch nothing (a fingerprint refresh is
      not a binding change and stays the orchestrator's business).
    * ``BIND`` — bind ``spool_id`` here. The writer's move semantics displace any
      incumbent, fleet-wide (one spool ⇔ at most one slot).
    * ``MINT`` — create a new spool row from ``mint_spec`` and bind it.
      ``mint_spec["replace_existing"]`` is True when a bound, different row must be
      unlinked first.
    * ``RECLAIM`` — ordinal-PRESERVING rebind of an existing row (doctrine rule 7:
      a mid-life re-seat keeps its FIFO position, so ``loaded_at`` must NOT be
      re-stamped).
    * ``RELEASE`` — unbind: the slot is empty and the roll's location claim is no
      longer true (operator ruling 1 — assignment = physical location truth).
    * ``REPLACE_SPENT`` — the W1 silent spent→mint: archive the drained row and mint
      its replacement, no prompt (the runout already proved the roll is gone).
    * ``DEFER`` — do nothing THIS push; a wire-safety input or an unresolved
      identity says now is the wrong moment. Always safe to repeat.
    * ``NONE`` — nothing to do, and nothing owed to the binding writer.
    """

    KEEP = "keep"
    BIND = "bind"
    MINT = "mint"
    RECLAIM = "reclaim"
    RELEASE = "release"
    REPLACE_SPENT = "replace_spent"
    DEFER = "defer"
    NONE = "none"


#: Every ``Decision.reason`` :func:`resolve` can emit — a PUBLIC contract, because the
#: orchestrator branches on these strings (a sibling KEEP is INFO-logged, the two
#: ``*_owed_full_read`` defers drive the idle-identify lane, ``unknown_tag_prompt_owed``
#: raises the durable operator prompt). Keep it in step with the table: a reason the
#: table can emit but this set does not name is a silently unhandled branch downstream.
RESOLUTION_REASONS = frozenset(
    {
        # Row 1 — wire safety.
        "ams_drying",
        "identify_in_flight",
        "insertion_settling",
        # Row 2 — the identity lane (uuid-primary).
        "identity_presence_conflict",
        "identity_matches_bound",
        "sibling_tag_read",
        "identity_ambiguous_owed_full_read",
        "identity_resolved_candidate",
        "partial_identity_owed_full_read",
        "pre_configured_apply_identity",
        "identity_claims_untagged_row",
        "unknown_identity_auto_add",
        "unknown_tag_prompt_owed",
        # Row 3 — empty on the wire.
        "empty_unbound",
        "spent_latch_on_empty",
        "pre_configured_awaiting_insert",
        "cleared_tray",
        # Row 4 — the tagless lane.
        "spent_swap_confirmed",
        "spent_latch",
        "pre_configured_apply",
        "tagged_row_awaits_tag_lane",
        "last_location_reclaim",
        "tagless_mint",
        "archived_bound_replaced",
        "fingerprint_matches",
        "different_filament",
        # Rows 5/6 — unresolved identity.
        "mid_print_unresolved",
        "identity_unresolved",
        "no_signal",
    }
)


@dataclass(frozen=True)
class BindingView:
    """A read-only view of a spool row + its binding, as the caller resolved it.

    Used for three roles, all the same shape: the slot's CURRENT binding, the
    ``identity_candidate``, and the ``last_location_candidate``. Keeping one shape
    means the table never has to care which query produced a row.

    ``fingerprint_type`` / ``fingerprint_color`` are "the filament this row stands
    for" — the assignment's fingerprint for a bound row, the spool's material/rgba
    for an unbound candidate. ``pre_configured`` mirrors
    ``spool_assignment.pre_configured_at is not None`` (W2 column; it replaces the
    fragile blank-fingerprint inference).
    """

    spool_id: int
    is_tagless: bool
    tag_uid: str | None
    tray_uuid: str | None
    spent: bool
    archived: bool
    fingerprint_type: str | None
    fingerprint_color: str | None
    pre_configured: bool


# One shape, three roles (see BindingView) — the alias documents intent at call sites.
SpoolView = BindingView


@dataclass(frozen=True)
class Decision:
    """The resolver's answer. Data only — performing it belongs to the writer."""

    kind: DecisionKind
    spool_id: int | None = None
    mint_spec: dict | None = None
    reason: str = ""


@dataclass(frozen=True)
class ResolutionContext:
    """Everything the table needs that is NOT in the observation, pre-resolved.

    The caller does the I/O; the table does the thinking. Wire-safety inputs
    (``drying``, ``identify_in_flight``, ``settling``) are authoritative INPUTS here
    and remain authoritative at publish time in the MQTT client (invariant 2:
    callers may defer, never pre-approve).
    """

    binding: BindingView | None = None
    # The row this observation's identity belongs to, resolved UUID-PRIMARY by the
    # caller: the row owning the observed ``tray_uuid`` first, the row owning the
    # observed ``tag_uid`` second. EXACT matches only — no suffix-LIKE, no first-char
    # variance widening (those belong to the tag resolver's own tolerant lane and must
    # never reach the table as if they were certainties). None = no row owns this
    # identity. The table re-checks whatever arrives against the observation
    # (:func:`identity_relation`), so a widened row cannot be smuggled in as a match.
    identity_candidate: SpoolView | None = None
    # An operator-created row that plausibly IS this newly-identified roll, so the tag
    # LANDS ON it instead of minting a stranger beside it. Resolved by the orchestrator
    # with strict priority:
    #
    # 1. the slot's own PRE-CONFIGURED binding's spool, when that spool carries no
    #    tag/uuid of its own — SpoolBuddy weigh-then-assign: the operator weighed a roll
    #    (label 750 g, say), bound it to the slot, and is now inserting it; the tag the
    #    AMS reads at insert belongs to THAT row, and minting a fresh 1000 g rfid_auto
    #    row would orphan the weight the operator just measured;
    # 2. otherwise ``spool_tag_matcher.find_matching_untagged_spool`` semantics — an
    #    unassigned, untagged, non-archived, fingerprint-compatible operator row (never
    #    an auto-minted ``ams_auto`` one), which is the attract-to-inventory lane the
    #    pre-cutover unknown-tag path ran BEFORE it ever considered minting.
    #
    # None = no such row; the table then mints/prompts exactly as before. The table
    # consumes this only under the same evidence gate as MINT (the full identity pair),
    # so a partial read can never land a tag on an operator's row.
    untagged_claim_candidate: SpoolView | None = None
    # Most-recent non-archived, non-spent, fingerprint-compatible spool whose last
    # location IS this slot (doctrine rule 7 reclaim donor).
    last_location_candidate: SpoolView | None = None
    # A physical roll cycle (≥ _MIN_PHYSICAL_ABSENT_S absence) is pending on this
    # slot — the ONLY thing that releases the W1 spent latch (doctrine rule 6:
    # duration is a flap filter, never identity).
    qualified_cycle_pending: bool = False
    auto_add_unknown: bool = True
    busy: bool = False  # printer RUNNING/PAUSE
    settling: bool = False  # mint/config settle window
    identify_in_flight: bool = False
    drying: bool = False
    tagless_default: dict | None = None


# --- state classification ---------------------------------------------------


def derive_state(obs: TrayObservation, binding: BindingView | None) -> SlotState:
    """Classify a slot from THIS push plus the durable binding facts.

    Order is load-bearing:

    1. A spent binding is ``SPENT_AWAITING_SWAP`` regardless of presence — that IS
       the W1 runout latch (the binding + ``spool.spent_at`` are its durable
       storage). Presence flaps at the runout instant, so keying the latch on
       presence would phantom-mint over a still-seated dead roll.
    2. A pre-configured binding with the tray not (yet) present is
       ``PRE_CONFIGURED`` — "awaiting insert", a deliberate bind-to-empty
       (SpoolBuddy weigh-then-assign). Once something IS present the slot is
       classified by what the wire says; the one-shot apply is a resolver row.
    3. Present → identified / assumed / unresolved by what the push asserted.
    4. Not present → EMPTY.
    5. Presence UNKNOWN → classify from what IS asserted; otherwise UNRESOLVED
       whenever anything signals occupancy OR a binding exists. EMPTY is only
       returned when there is no binding and no occupancy signal at all: an
       unknown must never be resolved toward EMPTY, because EMPTY is the state
       that authorizes releasing a binding.
    """
    if binding is not None and binding.spent:
        return SlotState.SPENT_AWAITING_SWAP
    if binding is not None and binding.pre_configured and obs.present is not True:
        return SlotState.PRE_CONFIGURED

    if obs.present is True:
        if obs.identity_asserted:
            return SlotState.OCCUPIED_IDENTIFIED
        if obs.config_nonempty:
            return SlotState.OCCUPIED_ASSUMED
        return SlotState.OCCUPIED_UNRESOLVED

    if obs.present is False:
        return SlotState.EMPTY

    # presence unknown
    if obs.identity_asserted:
        return SlotState.OCCUPIED_IDENTIFIED
    if obs.config_nonempty:
        return SlotState.OCCUPIED_ASSUMED
    if obs.occupancy_signal or binding is not None:
        return SlotState.OCCUPIED_UNRESOLVED
    return SlotState.EMPTY


# --- decision table ---------------------------------------------------------


def resolve(obs: TrayObservation, state: SlotState, ctx: ResolutionContext) -> Decision:
    """The ONE identity-resolution decision table. Pure; exhaustively parametrized.

    Rows are evaluated top to bottom. Every row states the doctrine rule or the
    incident it exists for.
    """
    binding = ctx.binding

    # -- Row 1: wire safety wins over everything -----------------------------
    # A guess published into a slot the firmware is mid-read destroys the answer
    # (doctrine rule 5 / cross-cutting invariant 2), and an AMS write during drying
    # disengages the tray and fails the cycle (HMS 0700_C069). These inputs are
    # authoritative here even when a perfect tag match is sitting on the wire —
    # deferring costs one push, guessing costs an incident.
    if ctx.drying:
        return Decision(DecisionKind.DEFER, reason="ams_drying")
    if ctx.identify_in_flight:
        return Decision(DecisionKind.DEFER, reason="identify_in_flight")
    if ctx.settling:
        # The insertion's first push (config seen, tag not yet read) must not mint a
        # row the firmware's own RFID read then destroys.
        return Decision(DecisionKind.DEFER, reason="insertion_settling")

    # -- Row 2: this push asserted an RFID identity --------------------------
    # RFID is the only identity oracle the farm has (doctrine rule 6), so it outranks
    # every fingerprint heuristic below — but the oracle speaks in TWO members and only
    # ``tray_uuid`` names the roll (module docstring: two chips, one uuid, verified on
    # 4/4 prod slots 2026-08-01). Hence the ordering here: prove sameness or difference
    # on the uuid, treat a tag-only disagreement as a question rather than an answer,
    # and require the FULL pair before minting a row that could be a twin.
    if obs.identity_asserted:
        if obs.present is False:
            # Contradiction: the cleared-tray shape AND a live tag in one push.
            # Resolve toward "wait for a clean push" — mutating on contradictory
            # evidence is how stale rows are born.
            return Decision(DecisionKind.NONE, reason="identity_presence_conflict")

        relation = identity_relation(binding, obs) if binding is not None else None

        # 2.1 — the bound row IS this roll (uuid agreement, or a plain tag agreement
        # when no uuid is in play). A spent tagged row also lands here: the respool
        # tiers (doctrine rule 3) are a CONSUMER the orchestrator runs on a KEEP —
        # this table never auto-respools.
        if binding is not None and relation == "same":
            return Decision(DecisionKind.KEEP, spool_id=binding.spool_id, reason=_keep_reason(binding, obs))

        # 2.2 — a tag-only disagreement against a binding that DOES claim an identity.
        # Sibling read or genuine swap? Unknowable from this push, and the two demand
        # opposite actions, so the table buys the answer instead of guessing it: an
        # idle identify yields a full read carrying both members, which lands on 2.1 or
        # 2.3 with certainty. NEVER mint here — that is what would duplicate a roll.
        # A binding that claims NO identity at all (a tagless row) is not ambiguous,
        # it simply has no claim to weigh, so it falls through to 2.3 and is displaced
        # by the tag now claiming its slot.
        if binding is not None and relation == "ambiguous" and (binding.tag_uid or binding.tray_uuid):
            return Decision(DecisionKind.DEFER, reason="identity_ambiguous_owed_full_read")

        # 2.3 — the binding is uuid-PROVEN to be a different roll, absent, or
        # identity-less. Whatever row owns this identity is the answer.
        candidate = ctx.identity_candidate
        if candidate is not None and identity_relation(candidate, obs) == "same":
            if binding is not None and candidate.spool_id == binding.spool_id:
                # The candidate lookup resolved back to the row already bound here
                # (e.g. the binding view carried no identity but the spool row does).
                return Decision(DecisionKind.KEEP, spool_id=binding.spool_id, reason=_keep_reason(candidate, obs))
            # A different row owns this identity → bind it. The writer's move semantics
            # displace any incumbent, here and fleet-wide (one spool ⇔ at most one
            # slot; 012-H2S layer-1 runout invariant).
            return Decision(DecisionKind.BIND, spool_id=candidate.spool_id, reason="identity_resolved_candidate")

        # Nothing owns this identity — or the candidate contradicts the wire, a caller
        # contract violation we refuse to act on as a match.
        if not (obs.tag_uid and obs.tray_uuid):
            # Partial identity with no owning row: a B-side tag read on a push that
            # carried no uuid is indistinguishable from a brand-new roll, and minting
            # on it is exactly how one roll becomes two rows. Wait for the full read.
            return Decision(DecisionKind.DEFER, reason="partial_identity_owed_full_read")

        # 2.4 — nothing OWNS this identity, but an operator row may still BE this roll.
        # Pre-cutover an unknown tag first tried ``find_matching_untagged_spool`` and,
        # on a hit, ``link_tag_to_inventory_spool`` moved the identity onto that row; a
        # bound slot never reached the mint lane at all, so a weigh-then-assign
        # pre-config row simply received its roll's tag at insert. Post-cutover the
        # table minted a fresh 1000 g ``rfid_auto`` row instead and orphaned the
        # operator's weighed one. This restores the attract lane, under the SAME
        # evidence gate as the MINT below (the full pair — the partial DEFER above
        # already returned), and ahead of both MINT and the prompt: a roll an operator
        # already logged is not an unknown roll, so there is nothing to auto-add and
        # nothing to ask about.
        #
        # A pre-config binding's spool carries no identity of its own, so 2.1 cannot
        # match it, 2.2's tagless-livelock gate deliberately lets it fall through, and
        # 2.3 finds no owner — which is exactly what routes it here.
        claim = ctx.untagged_claim_candidate
        if claim is not None:
            if binding is not None and binding.pre_configured and claim.spool_id == binding.spool_id:
                # The slot's own pre-configured row: the operator's intent and the
                # firmware's read are the same roll. The BIND is a same-spool, same-slot
                # upsert; the identity link + marker clear are the writer's business.
                return Decision(DecisionKind.BIND, spool_id=claim.spool_id, reason="pre_configured_apply_identity")
            return Decision(DecisionKind.BIND, spool_id=claim.spool_id, reason="identity_claims_untagged_row")

        if ctx.auto_add_unknown:
            # Full pair, unknown to the inventory → genuinely new. Both members are
            # recorded, so the next sibling read of this roll resolves on the uuid.
            return Decision(
                DecisionKind.MINT,
                mint_spec=_mint_spec_from_tray(obs, replace_existing=binding is not None),
                reason="unknown_identity_auto_add",
            )
        # Auto-add off: the operator prompt lane belongs to the orchestrator (a
        # durable prompt, not a WS-only broadcast — the pre-W1 gap).
        return Decision(DecisionKind.NONE, reason="unknown_tag_prompt_owed")

    # -- Row 3: the slot is EMPTY on the wire --------------------------------
    # Location truth (operator ruling 1): the assignment is a physical-location
    # claim, so a cleared tray releases it. Two exemptions, each load-bearing.
    if obs.present is False:
        if binding is None:
            return Decision(DecisionKind.NONE, reason="empty_unbound")
        if binding.spent:
            # W1 latch: the spent binding is the durable "this tray ran dry" state.
            # It survives the core physically leaving and self-clears at the next
            # qualified swap (row 4a) — not here.
            return Decision(DecisionKind.KEEP, spool_id=binding.spool_id, reason="spent_latch_on_empty")
        if binding.pre_configured:
            # Deliberate bind-to-empty ("awaiting insert") — releasing it would
            # delete the operator's intent.
            return Decision(DecisionKind.KEEP, spool_id=binding.spool_id, reason="pre_configured_awaiting_insert")
        # Live row (archived rows included — an archived row must never hold a
        # location claim): release. Fixes the five prod stale-on-absent bindings
        # (003-T2/140, 006-T2/166, 009-T3/96, 012-T3/156, 007C-A0T1/214).
        return Decision(DecisionKind.RELEASE, spool_id=binding.spool_id, reason="cleared_tray")

    # -- Row 4: tagless lane — configuration asserted, no identity -----------
    # "No tag" is the fleet's common case for third-party rolls (doctrine rule 2:
    # the tagless default is assumed unless a tag or an operator says otherwise).
    if obs.config_nonempty:
        # 4a — the W1 spent latch releases ONLY on a qualified physical cycle.
        # Tagless rows only: a spent TAGGED row belongs to the respool tiers
        # (doctrine rule 3), which the orchestrator runs off the tag lane.
        if state is SlotState.SPENT_AWAITING_SWAP and binding is not None and binding.is_tagless:
            if ctx.qualified_cycle_pending:
                return Decision(
                    DecisionKind.REPLACE_SPENT,
                    spool_id=binding.spool_id,
                    mint_spec=_tagless_mint_spec(obs, ctx, departed=binding),
                    reason="spent_swap_confirmed",
                )
            # No cycle: the runout-instant state flap must not phantom-mint over a
            # still-present dead roll.
            return Decision(DecisionKind.KEEP, spool_id=binding.spool_id, reason="spent_latch")

        # 4b — the pre-config one-shot: something got inserted into a slot the
        # operator pre-assigned, so apply that intent now (push config + stamp the
        # fingerprint + clear pre_configured_at — all writer-side).
        #
        # The gate is ``present is not False``, NOT ``present is True``: the A1-family
        # and P1S firmwares report a CONSTANT ``state=3`` (some pushes omit ``state``
        # altogether), so ``tray_observation._derive_present`` can only answer None for
        # them and a ``present is True`` gate left every one of those dialects "awaiting
        # insert" FOREVER — the roll physically seated, configured, and never applied
        # (upstream #1322; the pre-cutover replay handled exactly this shape).
        # Reaching row 4 already proves ``present is not False`` twice over — row 3 owns
        # every present-False push, and the cleared shape that makes presence False
        # asserts an EMPTY ``tray_type`` while this row needs a non-empty one — so the
        # KEEP arm that used to sit here was unreachable and is deleted rather than left
        # as a branch no push can take. Awaiting-insert is row 3's / row 5's answer.
        if binding is not None and binding.pre_configured and obs.present is not False:
            return Decision(DecisionKind.BIND, spool_id=binding.spool_id, reason="pre_configured_apply")

        # 4b′ — the row bound here is TAGGED, but this push carried no RFID fields.
        # That is not evidence the roll left: periodic AMS pushes routinely omit
        # identity (which is exactly why the merge preserves it), and a tagged row
        # is only ever re-decided by the tag lane or by the empty shape at row 3.
        # Mirrors the pre-cutover tagless branch tree's (2) "not ours" return.
        if binding is not None and not binding.is_tagless:
            return Decision(DecisionKind.KEEP, spool_id=binding.spool_id, reason="tagged_row_awaits_tag_lane")

        if binding is None:
            # 4c — reclaim: this roll was pulled from THIS slot and came back
            # (maintenance/drying). Rebinding the same row preserves its grams AND
            # its FIFO position — the rebind must NOT re-stamp loaded_at
            # (doctrine rule 7).
            if ctx.last_location_candidate is not None:
                return Decision(
                    DecisionKind.RECLAIM,
                    spool_id=ctx.last_location_candidate.spool_id,
                    reason="last_location_reclaim",
                )
            # 4d — nothing to reclaim: mint. (The settle gate that protects a fresh
            # mint from the firmware's pending RFID read already fired at row 1.)
            return Decision(DecisionKind.MINT, mint_spec=_tagless_mint_spec(obs, ctx), reason="tagless_mint")

        # 4e/4f — bound row.
        if binding.archived:
            # An archived row is not a live occupant: it must never KEEP a present
            # tray, even on a matching fingerprint. Replace it rather than leaving
            # the slot claimed by a retired row.
            return Decision(
                DecisionKind.MINT,
                mint_spec=_tagless_mint_spec(obs, ctx, replace_existing=True),
                reason="archived_bound_replaced",
            )
        if _fingerprint_matches(binding, obs):
            # Same filament → same roll, as far as any oracle can tell (doctrine
            # rule 6: no timer distinguishes same-vs-different tagless roll). Write
            # nothing to the spool; operator edits are sacred.
            return Decision(DecisionKind.KEEP, spool_id=binding.spool_id, reason="fingerprint_matches")
        # Different filament → a genuinely different roll: unlink + mint.
        return Decision(
            DecisionKind.MINT,
            mint_spec=_tagless_mint_spec(obs, ctx, replace_existing=True),
            reason="different_filament",
        )

    # -- Rows 5/6: nothing asserted about identity OR configuration ----------
    # Exempt bindings keep their latch/intent even with no evidence this push.
    if binding is not None and binding.spent:
        return Decision(DecisionKind.KEEP, spool_id=binding.spool_id, reason="spent_latch")
    if binding is not None and binding.pre_configured:
        return Decision(DecisionKind.KEEP, spool_id=binding.spool_id, reason="pre_configured_awaiting_insert")

    if state is SlotState.OCCUPIED_UNRESOLVED:
        if ctx.busy:
            # Doctrine rule 5: mid-print inserts are never auto-read, and reading or
            # mutating a binding on unresolved identity is how grams land on stale
            # rows (the spool-37 class). Resolved by the terminal sweep / idle
            # identify instead.
            return Decision(DecisionKind.DEFER, reason="mid_print_unresolved")
        # Idle: the orchestrator owes this slot an identify. NO binding mutation
        # until identity is resolved.
        return Decision(DecisionKind.NONE, reason="identity_unresolved")

    if state is SlotState.EMPTY:
        # Presence unknown, no binding, no occupancy signal — nothing to do.
        return Decision(DecisionKind.NONE, reason="empty_unbound")
    return Decision(DecisionKind.NONE, reason="no_signal")


# --- helpers ----------------------------------------------------------------


def identity_relation(view: BindingView, obs: TrayObservation) -> str:
    """How a row's stored identity relates to what THIS push asserted.

    Returns one of three verdicts — ``"same"``, ``"different"``, ``"ambiguous"`` — and
    the asymmetry between the two identifiers is the whole point (module docstring):

    * **Both sides assert a ``tray_uuid``.** That member alone names the roll, so it
      decides outright: equal → ``"same"`` EVEN when the tags differ (a sibling read of
      the roll's other chip — 4/4 prod slots, 2026-08-01); unequal → ``"different"``,
      positive proof of another roll.
    * **Otherwise, both sides assert a ``tag_uid``.** Equal → ``"same"``: one chip is
      only ever fitted to one roll, so a tag agreement is a real identification.
      Unequal → ``"ambiguous"``, NOT a difference: the scanned chip may be the far side
      of the very roll this row stands for, and no evidence in this push separates that
      from a swap. Only a full read (both members) can.
    * **Nothing comparable** — one side asserts a member the other does not, or a
      tagless row asserts neither → ``"ambiguous"``: the honest answer is "no verdict".

    Only a member BOTH sides assert can decide anything; a missing member is unknown,
    never a mismatch. That is exactly why the observation layer's atomic-pair rule
    (a push asserts only what it carried) is load-bearing here.
    """
    view_uuid = (view.tray_uuid or "").upper()
    obs_uuid = (obs.tray_uuid or "").upper()
    if view_uuid and obs_uuid:
        return "same" if view_uuid == obs_uuid else "different"

    view_tag = (view.tag_uid or "").upper()
    obs_tag = (obs.tag_uid or "").upper()
    if view_tag and obs_tag:
        return "same" if view_tag == obs_tag else "ambiguous"

    return "ambiguous"


def _keep_reason(view: BindingView, obs: TrayObservation) -> str:
    """``"sibling_tag_read"`` when a KEEP spans a tag change, else the plain reason.

    The orchestrator INFO-logs the sibling case: it is the one KEEP where the stored
    identity visibly disagrees with the wire, so an operator reading the log must be
    able to see WHY it was still the same roll (the uuid matched).
    """
    if obs.tag_uid and view.tag_uid and obs.tag_uid.upper() != view.tag_uid.upper():
        return "sibling_tag_read"
    return "identity_matches_bound"


def _fingerprint_matches(view: BindingView, obs: TrayObservation) -> bool:
    """Same physical filament: colour within tolerance AND same canonical material.

    Delegates to the two existing one-origin helpers (``colors_similar``,
    ``canonical_filament_type``) so the tagless fingerprint means exactly what it
    means everywhere else in the fork.
    """
    if not obs.config_nonempty:
        return False
    if not colors_similar(obs.tray_color or "", view.fingerprint_color or ""):
        return False
    return canonical_filament_type(obs.tray_type or "") == canonical_filament_type(view.fingerprint_type or "")


def _mint_spec_from_tray(obs: TrayObservation, *, replace_existing: bool = False) -> dict:
    """Mint spec for a row minted from THIS push's asserted fields (tagged lane)."""
    return {
        "source": "tray",
        "tag_uid": obs.tag_uid,
        "tray_uuid": obs.tray_uuid,
        "tray_type": obs.tray_type,
        "tray_color": obs.tray_color,
        "tray_info_idx": obs.tray_info_idx,
        "tray_sub_brands": obs.tray_sub_brands,
        "remain": obs.remain,
        "nozzle_temp_min": obs.nozzle_temp_min,
        "nozzle_temp_max": obs.nozzle_temp_max,
        "replace_existing": replace_existing,
    }


def _tagless_mint_spec(
    obs: TrayObservation,
    ctx: ResolutionContext,
    *,
    departed: BindingView | None = None,
    replace_existing: bool = False,
) -> dict:
    """Mint spec for the tagless lane, choosing tray-identity vs the default.

    Mirrors ``spool_tagless._replace_row_after_cycle``: mint from the configured
    tagless default when the tray is bare OR still carries the DEPARTED row's
    config (firmware leftover after a runout — a physically fresh roll must get a
    clean 4-dimension identity), else mint from the tray's own, genuinely
    different, configuration.
    """
    use_default = ctx.tagless_default is not None and (
        not obs.config_nonempty or (departed is not None and _fingerprint_matches(departed, obs))
    )
    if use_default:
        spec = {"source": "tagless_default", "default_filament": dict(ctx.tagless_default or {})}
        spec["replace_existing"] = replace_existing
        return spec
    spec = _mint_spec_from_tray(obs, replace_existing=replace_existing)
    spec["source"] = "tray"
    return spec


def format_slot_event(
    printer_id: int,
    ams_id: int,
    tray_id: int,
    from_state: SlotState,
    to_state: SlotState,
    decision: Decision,
) -> str:
    """The ONE structured audit line for a slot transition.

    ``[slot-state] printer=3 A0T2 OCCUPIED_ASSUMED→EMPTY release spool=140
    reason=cleared_tray``

    The plan drops the ``slot_events`` table on purpose (operator ruling): this
    grammar through the existing log pipeline + the ``support/logs`` endpoint is
    the forensic record. Keep it greppable — the token order is the contract.
    """
    spool = decision.spool_id if decision.spool_id is not None else "-"
    return (
        f"[slot-state] printer={printer_id} A{ams_id}T{tray_id} "
        f"{from_state.value}→{to_state.value} {decision.kind.value} "
        f"spool={spool} reason={decision.reason or '-'}"
    )
