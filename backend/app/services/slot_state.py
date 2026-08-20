"""Derived AMS slot state + the ONE identity-resolution decision table (W1).

Three pure functions and their vocabulary:

* :func:`derive_state` — ``state = f(observation, binding)``. NOTHING is persisted:
  a stored copy would be a second source of truth that can drift from the wire,
  which is the disease this rebuild cures (plan §"Schema — NO NEW TABLES"). The
  durable facts the state is derived FROM already exist: the assignment row,
  ``spool.spent_at`` (the W1 runout latch), ``spool_assignment.pre_configured_at``.
* :func:`resolve` — the decision table. Flat, exhaustively commented, one row per
  situation. It returns a :class:`Decision`; it never performs it.
* :func:`post_state` — the state an APPLIED decision leaves behind. ``derive_state``
  is computed against the PRE-transition binding, so it is the LEFT side of the audit
  line and can never be its right side without printing a tautology.

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
from backend.app.utils.tag_normalization import tag_matches_row


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
    * ``RECLAIM`` — the DE-BOUNCE: an ordinal-PRESERVING rebind of the slot's own last
      occupant after a release the evidence says never physically happened (doctrine
      rule 7: a mid-life re-seat keeps its FIFO position, so ``loaded_at`` must NOT be
      re-stamped — and since 2026-08-19 the bind MOMENT is preserved too, because a
      de-bounce must not stamp the swap boundary the ledger reconciler reads).
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
        # A FINISHED roll is never a live occupant (doctrine rule 3, operator ruling 3):
        # its tag reading LOADED is a fresh roll on a reused core. Row 2.0 emits this when
        # the finished row HOLDS the slot — retire it, mint its successor. When it merely
        # OWNS the tag (row 2.3a) there is no reason of its own: the row is discarded as a
        # candidate and the ordinary unowned-identity lane decides, so what surfaces is
        # that lane's reason (``partial_identity_owed_full_read`` /
        # ``unknown_identity_auto_add`` / ``unknown_tag_prompt_owed``).
        "reused_core_swap",
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
        "reseat_debounce",
        "tagless_mint",
        "runout_suspect_mint",
        # Row 3½ — the operator's re-check (doctrine rule 12), concluded on the ANSWER.
        "operator_recheck_mint",
        "operator_recheck_swap",
        "archived_bound_replaced",
        "fingerprint_matches",
        "different_filament",
        # Rows 5/6 — unresolved identity.
        "mid_print_unresolved",
        "identity_unresolved",
        # A spent-latched binding under a tray that is PRESENT but says nothing: the
        # roll seated there is a newcomer the farm has never named, so the slot owes a
        # discovery read rather than another silent latch KEEP.
        "spent_occupied_owed_identify",
        # …and the answer to that read, when it comes back NO TAG over a spent TAGGED
        # binding: hardware-certain different roll — swap to the tagless default.
        "spent_swap_no_tag_read",
        # The same proof over a LIVE tagged binding (doctrine rule 11, 2026-08-19): the
        # roll that left is not exhausted, so it is unlinked and keeps its grams while a
        # fresh tagless row takes the slot. Rows 4b′ and 5b.
        "tagged_swap_no_tag_read",
        "no_signal",
    }
)

#: The subset of :data:`RESOLUTION_REASONS` whose APPLIED decision leaves the slot
#: positively identified — row 2, the RFID lane, where the wire asserted an identity and
#: the decision resolves it onto a row. Every other binding decision is an assumption,
#: however good: the tagless pre-config apply, a fingerprint mint, a re-seat de-bounce
#: and a spent replacement all bind a row nothing has READ in that slot.
#:
#: Keyed on the reason rather than a new :class:`Decision` field because the reason set
#: is ALREADY this module's public contract with the orchestrator, which distinguishes
#: this very lane the same way (``slot_pipeline._IDENTITY_CLAIM_REASONS``,
#: ``_IDENTIFY_VERDICT``). The one MINT listed here, ``unknown_identity_auto_add``, is
#: exactly the mint whose ``mint_spec`` carries the full read pair — the equivalence is
#: pinned in the tests so the two views cannot drift. Consumed only by
#: :func:`post_state`; extend it in the same commit that adds an identity-lane reason.
IDENTITY_BACKED_REASONS: frozenset[str] = frozenset(
    {
        "identity_resolved_candidate",
        "pre_configured_apply_identity",
        "identity_claims_untagged_row",
        "unknown_identity_auto_add",
    }
)


@dataclass(frozen=True)
class BindingView:
    """A read-only view of a spool row + its binding, as the caller resolved it.

    Used for three roles, all the same shape: the slot's CURRENT binding, the
    ``identity_candidate``, and the ``debounce_candidate``. Keeping one shape means
    the table never has to care which query produced a row.

    ``fingerprint_type`` / ``fingerprint_color`` are "the filament this row stands
    for" — the assignment's fingerprint for a bound row, the spool's material/rgba
    for an unbound candidate. ``pre_configured`` mirrors
    ``spool_assignment.pre_configured_at is not None`` (W2 column; it replaces the
    fragile blank-fingerprint inference).
    """

    spool_id: int
    is_tagless: bool
    tag_uid: str | None
    # The roll's OTHER RFID chip, once sighted (``spool.sibling_tag_uid``). A row's tag
    # identity is a PAIR — never compare against ``tag_uid`` alone; go through
    # :func:`tag_matches_row`.
    sibling_tag_uid: str | None
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
    # The slot's SINGLE last occupant, when it is still a legitimate de-bounce donor:
    # the newest row released FROM this slot, adjudicated and never scanned past, and
    # only when it is non-archived, non-spent, UNTAGGED and fingerprint-compatible with
    # what the wire now reports (``slot_pipeline._debounce_candidate``). None = there is
    # nothing to de-bounce onto and the table mints.
    debounce_candidate: SpoolView | None = None
    # The slot's last presence GAIN returned a roll after a MEASURED absence shorter
    # than ``ams_presence._RESEAT_WINDOW_S`` — i.e. the release the farm acted on is
    # explicable as a SPURIOUS one. False for an absence whose start was never observed
    # (UNKNOWN is not "short") and for every longer gap. Computed OUTSIDE the table so
    # this stays a pure function of its inputs.
    reseat_within_window: bool = False
    # …and the CAUSE veto: this slot's release has a PHYSICAL explanation, so it was
    # never a glitch. True when the slot was the active feeder of a live print at the
    # loss edge (the ~3-minute bay-clear→HMS gap), or when the firmware is standing on
    # a runout for it right now (an open runout incident, a live demand, a standing
    # runout code). Also computed outside the table.
    runout_suspect: bool = False
    # A physical roll cycle (≥ _MIN_PHYSICAL_ABSENT_S absence) is pending on this
    # slot — the ONLY thing that releases the W1 spent latch (doctrine rule 6:
    # duration is a flap filter, never identity).
    qualified_cycle_pending: bool = False
    # A commanded DISCOVERY read on this slot ANSWERED "no tag" while something is still
    # seated (``ams_presence.read_answered_no_tag`` — the read economy owns the stamps,
    # the observation lane supplies the tray facts). Hardware evidence, not a timer: the
    # firmware was asked what is in the slot and said "nothing with a chip".
    #
    # Computed for ANY binding that CLAIMS a tag (doctrine rule 11, 2026-08-19) — spent or
    # live, bare tray or configured — and consumed by the four arms
    # :func:`_no_tag_answer_contradicts` gates. It was scoped to the spent+bare
    # constellation until this wave, which is why scenario G7 (a Bambu roll swapped for a
    # third-party one, the tray reporting configuration and no tag) persisted silently with
    # the wrong row bound. False everywhere else, and False for "not yet asked": silence is
    # not an answer (scenario G10).
    no_tag_read_answered: bool = False
    # The operator pressed "Re-check slot" on this slot (doctrine rule 12 — the click is
    # rule 6's own second identity oracle, "an RFID tag OR a human answer"), the durable
    # intent is still open, AND the tag-ness question has been ANSWERED: a commanded
    # discovery read came back finding no chip. Both halves are required and both are
    # resolved OUTSIDE the table (``slot_pipeline._operator_recheck_answered``), so this
    # stays a pure function of its inputs.
    #
    # "Answered, never the click alone" is the guard that makes the verb safe: mid-print
    # the farm never commands a read (rule 5), so a slot holding a brand-new or reused-tag
    # Bambu roll is indistinguishable from a tagless one, and minting tagless there is
    # precisely the "guess published into an unresolved slot destroys the firmware's
    # answer" failure (invariant 5). Until the answer lands this stays False and the slot
    # renders as the honest ``tray_unread`` interim state it already had.
    operator_recheck_answered: bool = False
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


def post_state(decision: Decision, derived: SlotState) -> SlotState:
    """The state the slot is in AFTER ``decision`` has been APPLIED.

    :func:`derive_state` answers "what do this push and the binding as it stood BEFORE
    the decision imply?" — the LEFT side of a transition. Using it as the right side too
    printed tautologies in production (``SPENT_AWAITING_SWAP→SPENT_AWAITING_SWAP
    replace_spent``, ``OCCUPIED_ASSUMED→OCCUPIED_ASSUMED bind``): the one line an
    operator reads to see what changed was hiding the change.

    ``derived`` is that pre-transition classification, returned unchanged for the kinds
    that write no binding. Total over :class:`DecisionKind` by construction — an
    unmapped kind RAISES rather than quietly reporting the pre-transition state, so a
    new kind fails loudly in the tests instead of silently reviving the tautology.
    """
    kind = decision.kind

    if kind is DecisionKind.RELEASE:
        # The binding is gone, and the only row that releases (row 3) requires a
        # wire-asserted empty tray — the orchestrator's orphan release says the same.
        return SlotState.EMPTY

    if kind is DecisionKind.BIND or kind is DecisionKind.MINT:
        # Identified only when an RFID identity was asserted and resolved onto the row
        # (see :data:`IDENTITY_BACKED_REASONS`); the tagless lane's binds and mints rest
        # on a fingerprint or an operator's intent, which is an assumption.
        if decision.reason in IDENTITY_BACKED_REASONS:
            return SlotState.OCCUPIED_IDENTIFIED
        return SlotState.OCCUPIED_ASSUMED

    if kind is DecisionKind.RECLAIM:
        # The de-bounce's donor is the slot's last occupant, admitted on a measured
        # sub-window absence with no physical cause. Nothing READ the roll, so the
        # rebind is assumption-tier however well the evidence rules out a swap.
        return SlotState.OCCUPIED_ASSUMED

    if kind is DecisionKind.REPLACE_SPENT:
        # The replacement is minted from the tagless default or the tray's own config —
        # assumed until something actually reads what now sits there.
        return SlotState.OCCUPIED_ASSUMED

    if kind is DecisionKind.KEEP or kind is DecisionKind.DEFER or kind is DecisionKind.NONE:
        # No binding was written, so the derived classification IS the after-state.
        return derived

    raise ValueError(f"post_state has no mapping for DecisionKind {kind!r}")


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

        # 2.0 — the identity resolves to a FINISHED roll, and the tray reads LOADED.
        # Doctrine rule 3 / operator ruling 3 (2026-08-19), scenario G3: "runout on an
        # RFID means g = 0, and any future read of the same tag is a new roll — how do you
        # add filament to 0 g? you can't, therefore a new roll on a reused core. It doesn't
        # need breadcrumbs." The physics is the whole argument: a roll the hardware
        # declared empty cannot come back holding filament, so the tag is on a DIFFERENT
        # roll. That is CONCLUDED from evidence, never asked.
        #
        # This row used to not exist and the lane was spent-BLIND in both directions: a
        # bound finished row fell to 2.1's KEEP (the slot kept describing filament that no
        # longer existed, and the fresh roll printed against a ledger reading 0 g
        # remaining), and an unbound one fell to 2.3's BIND — the ledger RESURRECTION that
        # incident shape 31 is made of, reached here through the tagged door instead of
        # the breadcrumb one.
        if _finished_roll_reading_loaded(binding, obs) and relation == "same":
            # The finished row HOLDS this slot, so retiring it and minting its successor is
            # one transition, not two — the same ``REPLACE_SPENT`` arm every other spent
            # roll swap uses, so the disposal (a pristine auto-mint is deleted, a
            # ledger-bearing row archived), the mint funnel and the binding writer stay one
            # lane. The departing row keeps its grams and its ``spent_at``: archiving is a
            # soft-hide, and the ledger stays raw (rule 8). Minting from the TRAY carries
            # the tag onto the successor, which is what makes the reused core resolvable on
            # the next push instead of leaving two active rows claiming one ``tray_uuid``.
            return Decision(
                DecisionKind.REPLACE_SPENT,
                spool_id=binding.spool_id,
                mint_spec=_mint_spec_from_tray(obs),
                reason="reused_core_swap",
            )

        # 2.1 — the bound row IS this roll (uuid agreement, or a plain tag agreement
        # when no uuid is in play). A LIVE tagged row lands here and KEEPs, unchanged —
        # 2.0 above took the finished ones, and only those. The respool tiers (doctrine
        # rule 3) remain a CONSUMER the orchestrator runs on a KEEP; what this table now
        # refuses to do is treat a FINISHED roll as a live occupant.
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
        if candidate is not None and candidate.spent:
            # 2.3a — the row that owns this identity is a FINISHED roll (the second half
            # of ruling 3, and the branch that had no test in either direction). It is the
            # normal post-runout shape for a tagged roll: the AMS clears the drained
            # slot's exist bit ~3 min BEFORE it declares the runout, so the binding is
            # released first and the spent stamp lands on an UNBOUND row (the 2026-08-13
            # tier-2 attribution). The operator then puts a fresh roll on the reused core,
            # the tag reads, and this lane used to BIND the drained row straight back onto
            # the slot — the fresh roll instantly reading 0 g remaining and staging every
            # run behind it.
            #
            # A finished roll is not a bindable owner, so it is treated as NO owner and the
            # lane below decides — under exactly the gates every other unowned identity
            # meets. That is deliberate, and it is what makes G3's "mint a fresh row
            # carrying the tag" ONE lane instead of a second copy of the mint written here:
            #
            # * a PARTIAL read still buys a full one first (2.3's DEFER) — minting on a
            #   half-read identity is precisely how one roll becomes two rows, and a
            #   reused core has no exemption from that;
            # * ``auto_add_unknown`` still decides mint-vs-prompt (2.5/2.6). The
            #   CONCLUSION "this is a different roll" is never asked (ruling 3); whether
            #   the farm may create inventory rows unattended is a separate operator
            #   policy that applies to every unknown roll.
            #
            # ``candidate.spent`` — the view's mirror of ``Spool.is_finished_roll`` — and
            # NOT :func:`_finished_roll_reading_loaded`: presence gates the RETIRE in 2.0,
            # where acting on an unknown would destroy a ledger row. REFUSING to bind
            # costs nothing in any presence state, and the refusal is the fail-safe half
            # of invariant 11's asymmetry — a bind not made self-heals on the next push,
            # a resurrected spent row is permanent.
            #
            # The table mutates nothing: the finished row keeps its grams, its ``spent_at``
            # and its tag. Retiring it belongs to the writer, at the moment the successor
            # actually lands — ``slot_pipeline._apply_mint``, whose mint existence recheck
            # must skip finished rolls for this same reason or it would convert the
            # successor's MINT straight back into the resurrection.
            candidate = None
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

    # -- Row 3½: the operator answered ---------------------------------------
    # Doctrine rule 12 (operator-ratified 2026-08-19, incident shape 32): the re-check
    # click is EVIDENCE — nobody presses it unless something physically happened — and it
    # is rule 6's own second identity oracle ("an RFID tag **or a human answer**") finally
    # wired up. This amends nothing; it finishes something.
    #
    # ``operator_recheck_answered`` already carries BOTH halves of rule 12's safety
    # (see :class:`ResolutionContext`): an open durable intent, and a commanded discovery
    # read that ANSWERED no chip. So by the time the table sees it, the question "is this
    # a different roll?" has a human's yes and the firmware's "nothing with a tag here" —
    # which is strictly more evidence than the tagless lane below ever gets on its own.
    #
    # WHERE this sits is the design. AFTER row 1 (wire safety still wins — deferring costs
    # one push, guessing costs an incident), AFTER row 2 (a tag on the wire outranks any
    # human: scenario R4's "the identity lane decides" IS row 2 deciding), and AFTER row 3
    # (a roll pulled between the click and the answer releases; there is nothing to
    # re-check in an empty bay). BEFORE row 4, because the whole point is that every one
    # of row 4's tagless verdicts — the de-bounce, the fingerprint KEEP, the spent latch —
    # is an inference the operator has just overruled with better evidence.
    #
    # Three exclusions, each a doctrine rule rather than caution:
    #
    #  * a PRE-CONFIGURED binding is untouched (scenario T13): operator intent is never
    #    guessed over, and a pre-config bind-to-empty IS operator intent — row 4b applies
    #    it, and re-checking a slot cannot mean discarding the roll the operator assigned
    #    to it;
    #  * something must be SEATED. Row 3 owns ``present is False`` outright, and the
    #    endpoint refuses an empty slot up front (scenario R6), but presence UNKNOWN
    #    reaches here and must assert nothing — an unknown is never resolved toward action
    #    (invariant 3);
    #  * a mint spec must be DERIVABLE. With a bare tray and no configured tagless default
    #    the only row that could be minted is an identity-less husk, which is worse than
    #    the honest unresolved state the slot already renders.
    if (
        ctx.operator_recheck_answered
        and obs.present is not False
        and not (binding is not None and binding.pre_configured)
        and (ctx.tagless_default is not None or obs.config_nonempty)
    ):
        if binding is not None and binding.spent:
            # The spent-latch park (scenario R5), which otherwise waits for a physical
            # cycle that may never come. REPLACE_SPENT rather than MINT because the
            # drained row must be RETIRED, not merely displaced — the same arm every
            # other spent-roll swap uses, so the disposal, the mint funnel and the
            # binding writer stay one lane.
            return Decision(
                DecisionKind.REPLACE_SPENT,
                spool_id=binding.spool_id,
                mint_spec=_tagless_mint_spec(obs, ctx, departed=binding),
                reason="operator_recheck_swap",
            )
        # Scenarios R2/R3/R8. ``departed=binding`` makes the mint spec fall back to the
        # tagless default whenever the tray still carries the outgoing row's config —
        # firmware leftover after a swap — so the fresh roll gets a clean identity
        # instead of inheriting the description of the roll that left.
        #
        # R8 is the honest false positive this verb accepts by design: an operator who
        # re-seated the SAME roll after a jam clear gets a new row at label weight. It is
        # surfaced rather than hidden — the acknowledgement offers a one-click undo — and
        # it self-corrects at that roll's next runout.
        return Decision(
            DecisionKind.MINT,
            mint_spec=_tagless_mint_spec(obs, ctx, departed=binding, replace_existing=binding is not None),
            reason="operator_recheck_mint",
        )

    # -- Row 4: tagless lane — configuration asserted, no identity -----------
    # "No tag" is the fleet's common case for third-party rolls (doctrine rule 2:
    # the tagless default is assumed unless a tag or an operator says otherwise).
    if obs.config_nonempty:
        # 4a — the W1 spent latch releases ONLY on a qualified physical cycle, for a
        # spent binding of ANY tag-ness. The old "tagless rows only, a spent TAGGED row
        # belongs to the respool tiers (doctrine rule 3)" reasoning was the deadlock: the
        # tiers act when a TAG IS READ, which is ROW 2, not this row — and this row is
        # reached precisely when no tag was read. So a spent tagged row met neither lane
        # and latched its slot against the replacement roll forever (printer 4 tray 2,
        # 2026-08-07: spool 212, spent, 1121.5 g on a 1000 g label, holding a ~90 %-full
        # unread newcomer out of service for a full day). Rule 3 is untouched — a tag read
        # over this slot still lands on row 2.1's KEEP and the tiers still own it there.
        #
        # "Of ANY tag-ness" needs one honest qualification: the release SIGNAL only reaches
        # this row because ``spool_tagless._maybe_prompt_fresh_roll`` checks spent-ness
        # BEFORE tag-ness (fixed in this same wave) — a tagged spent binding whose cycle was
        # instead routed to the fresh-roll prompt would never present
        # ``qualified_cycle_pending`` here. And this row needs a CONFIGURED tray, so the
        # constellations with no config and no cycle at all — a bare tray under a spent
        # tagged binding, the 2026-08-07 spool 226 shape — cannot reach it: those resolve at
        # rows 5/6 through ``spent_swap_no_tag_read`` instead.
        if state is SlotState.SPENT_AWAITING_SWAP and binding is not None:
            if ctx.qualified_cycle_pending:
                return Decision(
                    DecisionKind.REPLACE_SPENT,
                    spool_id=binding.spool_id,
                    mint_spec=_tagless_mint_spec(obs, ctx, departed=binding),
                    reason="spent_swap_confirmed",
                )
            # 4a′ — no cycle, but the READ answered. Rule 11 (:func:`_no_tag_answer_contradicts`):
            # a commanded read that found no chip over a TAGGED spent row is positive proof
            # of a different roll, and it does not become less positive because the tray
            # happens to carry configuration. Row 5a concluded on exactly this evidence for
            # a BARE tray and this arm was its missing twin: the same slot, the same answer,
            # parked forever on ``spent_latch`` because the firmware had left the departed
            # roll's ``tray_type``/``tray_info_idx`` behind (the ordinary shape after a swap
            # — see ``_tagless_mint_spec``'s ``departed`` fallback, which exists for it).
            # The cycle arm stays FIRST: a measured physical event is the primary evidence
            # and keeps its own reason.
            if _no_tag_answer_contradicts(obs, ctx):
                return Decision(
                    DecisionKind.REPLACE_SPENT,
                    spool_id=binding.spool_id,
                    mint_spec=_tagless_mint_spec(obs, ctx, departed=binding),
                    reason="spent_swap_no_tag_read",
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
        # altogether), so ``tray_fields.tray_presence`` can only answer None for
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
        # Two very different things can produce that, and the whole of this row is
        # telling them apart: "not asked" versus "asked and answered" (doctrine rule 11).
        if binding is not None and not binding.is_tagless:
            # ASKED AND ANSWERED — scenario G7, the commonest physical swap on this
            # fleet and the one that used to persist silently with the wrong row bound:
            # a Bambu roll pulled, a third-party roll seated, the tray reporting
            # ``tray_type: "PETG"``, ``tray_info_idx: "GFG02"``, ``tag_uid: null``. The
            # evidence for it existed the whole time — a commanded discovery read came
            # back finding no chip — and row 5a's own comment already called that
            # "positive proof of a different roll, the same certainty class as a uuid
            # disagreement". It was simply confined to spent rows and bare trays. It is
            # not confined any more (:func:`_no_tag_answer_contradicts`).
            #
            # The departed row is NOT archived: it is a live roll that left this slot,
            # not an exhausted one, so it keeps its grams and its history and is merely
            # unlinked (``replace_existing``) — the same disposition ``different_filament``
            # gives a tagless row the fingerprint refutes. ``departed=binding`` makes the
            # mint fall back to the tagless default whenever the tray still carries the
            # OUTGOING roll's configuration (firmware leftover after a swap), so the fresh
            # roll gets a clean identity instead of inheriting the description of the roll
            # that left.
            #
            # A transient read failure on a genuinely tagged roll self-corrects: the next
            # successful read finds the tag and row 2 displaces the never-fed minted row
            # (the same asymmetry ``read_answered_no_tag`` documents — the settle floor and
            # the in-flight gate are what make the answer worth this much).
            if _no_tag_answer_contradicts(obs, ctx):
                return Decision(
                    DecisionKind.MINT,
                    mint_spec=_tagless_mint_spec(obs, ctx, departed=binding, replace_existing=True),
                    reason="tagged_swap_no_tag_read",
                )
            # NOT ASKED — silence, scenario G10. A periodic AMS push routinely omits
            # identity (which is exactly why the merge preserves it), so this is no
            # evidence the roll left, and a tagged row is otherwise only ever re-decided
            # by the tag lane or by the empty shape at row 3. Mirrors the pre-cutover
            # tagless branch tree's (2) "not ours" return.
            return Decision(DecisionKind.KEEP, spool_id=binding.spool_id, reason="tagged_row_awaits_tag_lane")

        if binding is None:
            # 4c — DE-BOUNCE: the release this slot acted on was most likely SPURIOUS,
            # so the roll never left and its row keeps its grams AND its FIFO position
            # (doctrine rule 7 — the rebind must not re-stamp ``loaded_at``).
            #
            # THIS LANE IS A GLITCH FILTER, NOT AN IDENTITY ORACLE (rule 7 as amended
            # 2026-08-19, operator-ratified). It used to fire on nothing but a
            # ``last_location_*`` breadcrumb plus a fingerprint match — and on this
            # fleet every roll is black PETG reporting ``tag_uid: null, tray_uuid:
            # null, remain: -1``, so that test is ALWAYS TRUE against residue that is
            # never cleared and has no age bound. 002/005-H2S, 2026-08-19: two AMS
            # units sat empty for three days, one fresh roll went into each, and the
            # farm re-attached spool 292 (954.4 g used of 1000) and spool 298 (936.1 g)
            # to them. Both landed under the 150 g start floor and staged whole
            # production runs behind a row describing filament that no longer existed.
            #
            # It cannot simply be deleted either: 14 of 52 reclaims in 8 days returned
            # inside 5 minutes (four at 0.0 min across four printers inside ONE minute),
            # which no human produces — those are SPURIOUS releases this lane silently
            # repairs, and minting on them would reset a part-used roll to a full label
            # and bypass the start floor several times a day, unattended.
            #
            # So it is SCOPED. Four of the five conditions are the caller's
            # (``slot_pipeline._debounce_candidate``: the slot's single last occupant,
            # adjudicated and never scanned past; untagged, because a tagged roll
            # re-asserts its own identity and a breadcrumb can only pre-empt it;
            # non-spent, non-archived; fingerprint-compatible). The two here are the
            # ones a REVIEWER must be able to see in the table:
            #
            #  * ``reseat_within_window`` — the absence was MEASURED and short. Duration
            #    decides no identity POSITIVELY: outside the window the farm asserts
            #    nothing and MINTS, which is the whole of what timing may say.
            #  * ``runout_suspect`` — decided by CAUSE, never by timing (rule 6 /
            #    invariant 6). A runout release is never a glitch, and the AMS clears a
            #    drained slot's exist bit ~3 min BEFORE it declares the runout, so a
            #    refill made inside that gap is exactly a FAST return onto a row that is
            #    exhausted but not yet ``spent_at``-stamped (the 08-13 resurrection,
            #    shape 31). Without this veto, scoping the window would make that case
            #    the DOMINANT surviving reclaim instead of one among many.
            donor = ctx.debounce_candidate
            if donor is not None and ctx.reseat_within_window:
                if ctx.runout_suspect:
                    # The window would have admitted this one; the cause refuses it. Its
                    # own reason, because the COUNT of these is how the farm confirms
                    # T7/T8 are being caught rather than silently never firing.
                    return Decision(
                        DecisionKind.MINT, mint_spec=_tagless_mint_spec(obs, ctx), reason="runout_suspect_mint"
                    )
                return Decision(DecisionKind.RECLAIM, spool_id=donor.spool_id, reason="reseat_debounce")
            # 4d — nothing to de-bounce onto: mint. (The settle gate that protects a
            # fresh mint from the firmware's pending RFID read already fired at row 1.)
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
        if obs.present is True:
            # Spent latch, but something IS seated (state 10/11) and this push asserted
            # neither identity nor configuration for it. The drained roll's own state
            # cannot explain a present tray forever: what sits there is a roll the farm
            # has never named, and KEEPing the latch is what made the slot un-nameable —
            # nothing owed an identify, so the newcomer's tag was never read (printer 4
            # tray 2, 2026-08-07). Owe the read instead; the binding is still untouched
            # here, exactly as row 5's unresolved arm leaves it.
            #
            # Presence must be the TRI-STATE True: this arm exists to earn a discovery
            # read, and ``ams_presence.identify_needed``'s spent-occupied arm now grants
            # one on exactly that — either present state, the same rule
            # ``tray_fields.tray_presence`` applies here. The two were misaligned (the
            # need authority demanded state 10 specifically), so this table emitted a
            # reason for every state-11 spent-occupied slot that the authority could
            # never grant: a request standing forever, resolving nothing. A verdict
            # emitted on evidence the need authority does not accept is not a read, it is
            # a loop with no exit. Wire safety for a threaded-on tray is enforced where
            # the rest of it lives — ``command_identify`` defers on engaged filament and
            # spends nothing, so the entitlement waits for the idle edge.

            # 5a — the ANSWER to that discovery read, when it came back NO TAG. The arm
            # above only ever handled the tag-FOUND outcome (a tag lands, row 2 decides);
            # the no-tag answer — the expected one for every tagless roll — concluded
            # NOTHING, so the slot went on re-owing a read it had already been given and
            # parked forever (2026-08-07, spool 226 on 001-H2S slot 1: spent + tagged
            # binding, a fresh TAGLESS roll seated in the slot, the read answered no-tag at
            # 20:03 and the slot was still latched a day later). A commanded read that
            # found no chip over a binding that HAS one is positive proof of a different
            # roll — the same certainty class as a uuid disagreement — so it resolves here
            # instead of waiting for evidence that can never arrive: the tray is bare, so
            # row 4's cycle machinery is unreachable by construction.
            #
            # Since 2026-08-19 (doctrine rule 11) this arm is ONE CALLER of the general
            # predicate rather than its only definition — the conclusion is unchanged and
            # its guards are unchanged, they simply live in
            # :func:`_no_tag_answer_contradicts` now, beside the three other constellations
            # the same proof licenses. Scenario G9 (a spent TAGLESS binding) is still
            # excluded, and still for the reason this row first gave: over a claim-less
            # binding a no-tag read proves nothing at all, because the same core reads the
            # same way before and after a swap. That case stays with the qualified-cycle
            # machinery, which measures a PHYSICAL event instead.
            #
            # ``tagless_default`` stays a LOCAL guard: this row is reached only on a BARE
            # tray, which carries no fields to mint the replacement from, so with no default
            # configured there is nothing to bind and the slot keeps owing its read. Row 4b′
            # needs no such guard — a configured tray can always mint from itself.
            if _no_tag_answer_contradicts(obs, ctx) and ctx.tagless_default is not None:
                return Decision(
                    DecisionKind.REPLACE_SPENT,
                    spool_id=binding.spool_id,
                    mint_spec=_tagless_mint_spec(obs, ctx, departed=binding),
                    reason="spent_swap_no_tag_read",
                )
            if ctx.busy:
                # Doctrine rule 5: mid-print inserts are never auto-read.
                return Decision(DecisionKind.DEFER, reason="mid_print_unresolved")
            return Decision(DecisionKind.NONE, reason="spent_occupied_owed_identify")
        # Presence UNKNOWN (a push that said nothing about the slot): the latch is the
        # durable state and an unknown is never resolved toward action. Presence False
        # never reaches here — row 3 owns it, latch exemption included.
        return Decision(DecisionKind.KEEP, spool_id=binding.spool_id, reason="spent_latch")
    if binding is not None and binding.pre_configured:
        return Decision(DecisionKind.KEEP, spool_id=binding.spool_id, reason="pre_configured_awaiting_insert")

    # 5b — row 5a's mirror for a LIVE tagged binding: same bare tray, same answered
    # no-tag read, same proof (doctrine rule 11) — only the departed row is a roll that
    # LEFT rather than one that ran dry, so it is unlinked and keeps its grams instead of
    # being archived. Without this arm the constellation fell to ``identity_unresolved``
    # and re-owed a read it had already been given — the exact silence that parked spool
    # 226 for a day (observed-incidents shape 25), reproduced one binding-state over.
    # ``close_answered_read`` now spends the entitlements on that answer, so the slot
    # would not even go on asking: it would simply sit wrong and quiet.
    #
    # Ordered AFTER the pre-configured KEEP on purpose: operator intent is never guessed
    # over (scenario T13), so a slot awaiting a pre-assigned roll owes this nothing — the
    # same exclusion ``_operator_recheck_answered`` makes for the same reason.
    if _no_tag_answer_contradicts(obs, ctx) and ctx.tagless_default is not None:
        return Decision(
            DecisionKind.MINT,
            mint_spec=_tagless_mint_spec(obs, ctx, departed=binding, replace_existing=True),
            reason="tagged_swap_no_tag_read",
        )

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
    * **Otherwise, both sides assert a ``tag_uid``.** Matching EITHER of the row's two
      chips → ``"same"``: one chip is only ever fitted to one roll, so a tag agreement
      is a real identification — and once the sibling has been sighted and persisted,
      the roll's far side is just as much an identification as its near side.
      Matching neither → ``"ambiguous"``, NOT a difference: the scanned chip may still
      be a side of this roll we have never read, and no evidence in this push separates
      that from a swap. Only a full read (both members) can.
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

    if (view.tag_uid or view.sibling_tag_uid) and obs.tag_uid:
        return "same" if tag_matches_row(obs.tag_uid, view.tag_uid, view.sibling_tag_uid) else "ambiguous"

    return "ambiguous"


def _keep_reason(view: BindingView, obs: TrayObservation) -> str:
    """``"sibling_tag_read"`` when a KEEP spans an UNRECORDED tag, else the plain reason.

    The orchestrator INFO-logs the sibling case and PERSISTS the scanned chip onto the
    row: it is the one KEEP where the stored identity visibly disagrees with the wire,
    so an operator reading the log must be able to see WHY it was still the same roll
    (the uuid matched).

    Once that second chip is on the row, a re-read of it matches the PAIR
    (:func:`tag_matches_row`) and this returns the plain reason — which is what makes
    the announcement fire once per roll ever instead of on every push of every restart
    (six prod spools replayed it forever before the pair was persisted).
    """
    if obs.tag_uid and view.tag_uid and not tag_matches_row(obs.tag_uid, view.tag_uid, view.sibling_tag_uid):
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


def _finished_roll_reading_loaded(view: SpoolView | None, obs: TrayObservation) -> bool:
    """G3's RETIRE evidence: a FINISHED roll whose tray reads LOADED.

    Doctrine rule 3 / operator ruling 3 (2026-08-19). A runout means the row reached
    zero, and filament cannot be added to a 0 g roll — so the same tag reading back over
    a SEATED tray is a new roll on a reused core, with the certainty of a physical
    impossibility rather than of a heuristic. Row **2.0** acts on it: the finished row
    HOLDS the slot → ``REPLACE_SPENT``, retire it and mint its successor carrying the tag.

    ``view.spent`` is this table's mirror of ``Spool.is_finished_roll`` — the ONE model
    -side encoding of "a spent row is a FINISHED roll", which the untagged attract lane
    reads as an EXCLUSION and the tagged lanes read as EVIDENCE. The view field is
    computed by the orchestrator so this function stays pure, exactly as
    ``BindingView.is_tagless`` mirrors ``spool_tagless.is_tagless_spool``.

    ``obs.present is True`` is the tri-state rule (invariant 3), and it is the table's
    form of "the tray reads loaded": presence UNKNOWN asserts nothing and must never
    resolve toward retiring a row, and presence FALSE never reaches row 2 at all (the
    identity-vs-cleared contradiction returns above). Erring LATE here costs one pushall;
    erring early would retire a row on a push that said nothing.

    Row 2.3a — the finished row merely OWNS the tag — deliberately does NOT come through
    here. It refuses to BIND rather than retiring anything, and a refusal is fail-safe in
    every presence state, so gating it on presence would only leave the resurrection door
    open on a push that carried no presence at all. It tests ``candidate.spent`` directly:
    same mirror, without the evidence clause this one adds for the mutation.
    """
    return view is not None and view.spent and obs.present is True


def _no_tag_answer_contradicts(obs: TrayObservation, ctx: ResolutionContext) -> bool:
    """A tagless roll can NEVER be an RFID roll — doctrine rule 11, in ONE place.

    A commanded discovery read that ANSWERED "no chip" over a binding that CLAIMS one is
    positive proof of a different roll: the same certainty class as a ``tray_uuid``
    disagreement, and it does not care what else is true of the row. The table used to say
    exactly this in row 5a's own comment and then confine the conclusion to one
    constellation (spent row, bare tray), so the commonest physical shape on this fleet — a
    Bambu roll swapped for a third-party one, which reports ``tray_type: "PETG"``,
    ``tray_info_idx: "GFG02"``, ``tag_uid: null`` — persisted silently with the wrong row
    bound (scenario G7). This predicate is the whole of rule 11's positive clause; the FOUR
    arms that call it are the several conclusions it licenses:

    * row 4a — spent binding, CONFIGURED tray → ``spent_swap_no_tag_read``;
    * row 4b′ — live binding, CONFIGURED tray → ``tagged_swap_no_tag_read`` (G7);
    * row 5a — spent binding, BARE tray → ``spent_swap_no_tag_read`` (the original,
      2026-08-07 spool 226 / 001-H2S slot 1; observed-incidents shape 25);
    * rows 5/6 — live binding, BARE tray → ``tagged_swap_no_tag_read``.

    ONE-WAY BY LOGIC, NOT BY TIMIDITY, and the three refusals are each a doctrine rule:

    * ``binding.is_tagless`` — a binding that claims NO identity has nothing to contradict,
      and the same bare core reads identically before and after a swap (same-core
      ambiguity). Scenario **G9**: the arm that must NOT fire. That case keeps belonging to
      the qualified-cycle machinery, which measures a PHYSICAL event instead. Stated HERE,
      in the table, and NOT only in the caller that computes the read evidence
      (``slot_pipeline._no_tag_read_answered``, whose matching gate is a cheap exit that
      spares the ledger peek): the two express the same rule and neither is a redundant
      guard on the other — delete either and rule 11's one-way clause stops being
      assertable where it is decided.
    * ``obs.present is True`` — the tri-state (invariant 3). Nothing seated, nothing to be
      a different roll; an unknown is never resolved toward a binding write.
    * ``not ctx.busy`` — doctrine rule 5. Mid-print the farm commands no reads, so an
      answer still standing during a print predates it and says nothing about a roll
      inserted since. Believing it is how scenario R4's reused-tag RFID roll would be
      mis-minted as tagless.

    ``ctx.no_tag_read_answered`` is "asked AND answered", never "not asked" — a periodic
    push that merely omits the RFID fields is SILENCE and keeps the binding (scenario
    **G10**). That distinction lives in ``ams_presence.read_answered_no_tag``, which owns
    the read stamps and the settle floor; this table only consumes its verdict.
    """
    binding = ctx.binding
    if binding is None or binding.is_tagless:
        return False
    return ctx.no_tag_read_answered and obs.present is True and not ctx.busy


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
