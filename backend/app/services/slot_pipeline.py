"""Slot pipeline (W3a) — the ONE orchestrator that turns pure decisions into state.

``TrayObservation`` (W1) → :func:`slot_state.derive_state` → :func:`slot_state.resolve`
→ **this module** → ``spool_binding`` (the one binding writer). The decision table is
pure and I/O-free by contract; everything that reads the DB, touches the wire, or
writes a binding lives here, so there is exactly one place where a slot decision
becomes a fact.

What this module owns:

* **Candidate resolution** — the uuid-primary identity lookup and the last-location
  reclaim donor the table asks for. Both are STRICT: uuid ownership first
  (``spool_tag_matcher.find_spool_sharing_tray_uuid``), then EXACT tag equality. The
  tag resolver's tolerant variance lanes (``get_spool_by_tag``) are deliberately NOT
  used — their widening is for legacy callers, and a widened row reaching the table as
  if it were a certainty is the false-merge hazard the 2026-08-01 audit named. When
  NOTHING owns an asserted identity, one further lookup asks whether an operator row
  already IS this roll (:func:`_untagged_claim_candidate`) — the weigh-then-assign
  pre-config row, then ``find_matching_untagged_spool``'s attract lane — so the tag
  lands on that row instead of minting a 1000 g stranger beside it.
* **Context** — the wire-safety inputs (drying / identify-in-flight / settle windows)
  the table treats as authoritative, read-only. Per cross-cutting invariant 2 a caller
  may DEFER on them but never pre-approve a write: the client re-evaluates at publish
  time and that check is the only one that closes the race.
* **Application** — one branch per :class:`slot_state.DecisionKind`, each one mirroring
  the pre-cutover behaviour it replaces (cited inline), plus two idempotency guards a
  pure table cannot carry:
  1. a last-second **existence recheck** before every MINT: if a row already owns this
     identity (a race, or a caller that resolved candidates badly), the mint converts
     to a BIND of that row — one physical roll can never become two ledger rows;
  2. a per-pass **seen-set**: one spool may be applied at most once per push, which is
     the second half of the 007-H2C flip-flop fix (the writer's move damper is the
     first — ``spool_binding._MOVE_DAMPER_S``).

  Application also owns what happens to the row a bind/release leaves BEHIND
  (:func:`_dispose_displaced`, :func:`_dispose_ghost`): the writer unbinds an incumbent
  fleet-wide and says nothing about the row itself, so a never-fed auto-minted ghost and
  a displaced SPENT core would otherwise linger as active inventory forever.
* **Serialization** — one asyncio lock per printer, so two pushes for one printer can
  never interleave their read-decide-write windows. This IS the fork's assignment lock
  now: W3b deleted ``main.py``'s ``_get_ams_assignment_lock``, because with identity
  decided in exactly one place the lock belongs with the decider.

**Never raises** (cross-cutting invariant 10): every entry point is fully guarded and a
single poisoned slot logs ERROR and is skipped — the rest of the pass still runs. A
farm-side failure may not break the MQTT callback chain.

Audit trail: there is no events table (operator ruling). Every APPLIED transition emits
the one structured line :func:`slot_state.format_slot_event` produces, through the
existing log pipeline / ``support/logs`` endpoint. KEEP / DEFER / NONE are DEBUG — they
happen on every push of every slot and would drown the lane.

Feed (W3b): RAW pre-merge pushes, not the merged display state. ``bambu_mqtt``'s
``on_ams_push_raw`` hook fires inside ``_handle_ams_data`` BEFORE the tray merge;
``printer_manager`` builds the observations synchronously in that callback (owned frozen
objects — the merge may mutate the wire dicts the instant it returns) and schedules this
pass fire-and-forget. The merged payload stays display-only: its deliberate
never-clear-identity rule is what produced the 001-T3 chimera, so it must never reach a
binding decision. ``main.on_ams_change`` still runs, but only as a LEDGER/WIRE consumer
lane (respool gate, increase-only weight sync, K-profile drift, bare-tray config) — it
reads the binding this module writes and never decides identity.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from backend.app.core.websocket import ws_manager
from backend.app.models.printer_incident import KIND_RUNOUT, PrinterIncident
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import (
    ams_presence,
    hms_errors,
    printer_incidents,
    slot_recheck,
    spool_binding,
    spool_tagless,
)
from backend.app.services.slot_state import (
    BindingView,
    Decision,
    DecisionKind,
    ResolutionContext,
    SlotState,
    SpoolView,
    derive_state,
    format_slot_event,
    post_state,
    resolve,
)
from backend.app.services.spool_binding import NEVER_FED_MAX_G, bind_spool_to_slot, release_spool_from_slot
from backend.app.services.spool_respool import encode_global_tray
from backend.app.services.spool_tag_matcher import (
    create_spool_from_tray,
    find_matching_untagged_spool,
    find_spool_sharing_tray_uuid,
    link_tag_to_inventory_spool,
)
from backend.app.services.spool_tagless import is_tagless_spool
from backend.app.services.tray_fields import TRAY_PRESENT_STATES, parse_tray_exist_bits, slot_exist_bit
from backend.app.services.tray_observation import TrayObservation, observation_tray_dict
from backend.app.utils.color_utils import colors_similar
from backend.app.utils.filament_types import canonical_filament_type

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ``origin`` values this module passes to the binding writer. Log attribution only —
# the writer's single behavioural carve-out is ``spool_binding.OPERATOR_ORIGIN``, and
# no wire-driven origin may ever claim it (the move damper protects exactly these
# paths).
ORIGIN_BIND = "pipeline"
ORIGIN_RECLAIM = "pipeline_reclaim"
ORIGIN_MINT = "pipeline_mint"
ORIGIN_PRECONFIG = "pipeline_preconfig"
# An identity landing on an operator-created untagged inventory row (the attract lane).
# Its own origin so the writer's INFO trail distinguishes "the tag found its row" from a
# plain identity bind when an operator reads back why a row gained a tag.
ORIGIN_CLAIM = "pipeline_claim"

# The two decisions that LINK this push's identity onto the row they bind, instead of
# minting a stranger beside it. Both are BINDs, so they ride the ordinary bind path and
# only differ in the three extra things they owe: the identity link, the pre-config
# marker clear, and NO config push.
_IDENTITY_CLAIM_REASONS = frozenset({"pre_configured_apply_identity", "identity_claims_untagged_row"})

# The REPLACE_SPENT reason whose evidence is an ANSWERED commanded read rather than a
# qualified physical cycle (``slot_state`` row 5a, 2026-08-07 spool 226). The two differ in
# exactly two places inside :func:`_apply_replace_spent` — the tray shape they accept and
# whether a cycle is consumed — so the string is named once here instead of being spelled
# out at both.
_NO_TAG_SWAP_REASON = "spent_swap_no_tag_read"

# The operator's re-check verdicts (``slot_state`` row 3½, doctrine rule 12). Named here
# because three lanes below branch on them: the spent arm's evidence shape, the pending
# cycle's disposal, and the intent's own resolution.
_RECHECK_SWAP_REASON = "operator_recheck_swap"
_RECHECK_MINT_REASON = "operator_recheck_mint"
_RECHECK_REASONS = frozenset({_RECHECK_SWAP_REASON, _RECHECK_MINT_REASON})

# The G3 reused-core swap (``slot_state`` row 2.0): a FINISHED row holding the slot whose
# own tag reads back over a seated tray. Named here because the spent arm branches on it.
_REUSED_CORE_SWAP_REASON = "reused_core_swap"

# Spent-swap reasons whose proof is NOT a qualified physical cycle, so demanding one would
# veto the very swap they exist to perform. ``spent_swap_no_tag_read`` rests on an answered
# commanded read; ``operator_recheck_swap`` rests on that PLUS a human's answer;
# ``reused_core_swap`` rests on an IDENTITY READ, which is the strongest evidence the farm
# has — identity-tier evidence may displace ANY binding, while the physical cycle this set
# waives is assumption-tier (invariant 11). All three are therefore corroborated by
# observation-asserted presence rather than by ``tray_loaded``.
_CYCLELESS_SWAP_REASONS = frozenset({_NO_TAG_SWAP_REASON, _RECHECK_SWAP_REASON, _REUSED_CORE_SWAP_REASON})

# …and the subset of THOSE that still SPEND a pending cycle when one happens to exist. Both
# describe a swap a human physically performed, so an observed cycle on that slot is about
# THIS swap and must not survive to authorise a later one with nothing behind it.
# ``spent_swap_no_tag_read`` is deliberately absent: its tray is BARE by construction, which
# is the shape the cycle lane cannot reach, so it has nothing to spend.
_CYCLE_SPENDING_SWAP_REASONS = frozenset({_RECHECK_SWAP_REASON, _REUSED_CORE_SWAP_REASON})

# Outcomes that RETIRE the slot's pending physical cycle: both leave a DIFFERENT row
# holding the slot, so the swap evidence has been answered and may not be replayed by a
# later spent binding. See :func:`_settle_physical_cycle` for the full five-outcome table.
_CYCLE_DISCARDING_KINDS = frozenset({DecisionKind.MINT, DecisionKind.BIND})

# Orchestrator-side release reason. Deliberately NOT in ``slot_state.RESOLUTION_REASONS``:
# an assignment whose spool row is gone never reaches the table at all (there is nothing
# to build a BindingView from), so the table can neither emit nor own this reason.
# Mirrors the orphan drop the pre-cutover tagless branch tree performed, but through
# the ONE unbind writer so it still leaves a structured release line.
ORPHAN_RELEASE_REASON = "orphaned_assignment"

# Decision reason → the identify NEED verdict the discovery lane understands
# (``ams_presence.identify_needed``). Both ``*_owed_full_read`` defers arise on a push that
# ASSERTED a tag over a BOUND slot, so the slot is tagged and the read is guaranteed
# answerable — but "answerable" is not "owed", and the two differ in whether the push is
# NEW evidence.
#
# ``partial_identity_owed_full_read`` is: the push carried one member of the identity pair
# and not the other, which is a shape a single full read resolves outright.
#
# ``identity_ambiguous_owed_full_read`` is NOT. A tag disagreeing with a tag-bearing
# binding STANDS — the wire re-asserts it at ~1 Hz for as long as the roll sits there — so
# passing it straight through re-bought the same read on every push, with the client's
# identify gate as the only brake. It is need-GATED now, and the ambiguity EPISODE buys the
# occasion the need authority spends (``ams_presence.open_ambiguity_occasion``): one read
# per (bound row, disagreeing tag) pair, re-armed only by a genuinely different
# disagreement. While filament is engaged the read defers and the occasion is untouched, so
# the answer is bought on the idle edge instead of being lost.
#
# ``identity_unresolved`` is NOT evidence of anything. It arises with no identity
# asserted at all, and a slot can sit in it FOREVER (008-H2C AMS2 slot2, 2026-08-02: a
# dialect-odd state with a stuck exist bit resolved NONE(identity_unresolved) on every
# push, and the pipeline answered each one with a discovery identify — a permanent
# ~30 s read loop on an EMPTY slot, throttled only by the client's identify gate).
# Doctrine rule 3 / invariant 4: a discovery read is owed only for an UNANSWERED
# QUALIFIED PHYSICAL CYCLE (or a spent-latched slot under an unidentified seated roll),
# so this verdict is a REQUEST that :meth:`PipelineDeps.identify_verdict` must clear with
# ``ams_presence.identify_needed`` — the one need authority — before anything is spent.
#
# ``spent_occupied_owed_identify`` is the same shape: the table has seen a seated,
# unnamed roll over a spent latch, but WHETHER that earns a read is the predicate's call
# (its spent-occupied arm: state 10, no wire tag, an open read occasion — one attempt per
# occupancy epoch, because every failed read leaves a printer-side 0700_0081 that only a
# power-cycle clears).
_IDENTIFY_VERDICT = {
    "identity_ambiguous_owed_full_read": "rfid_refresh",
    "partial_identity_owed_full_read": "rfid_refresh",
    "identity_unresolved": "discovery",
    "spent_occupied_owed_identify": "discovery",
}

# The reasons in :data:`_IDENTIFY_VERDICT` that rest on STANDING evidence and must clear
# ``ams_presence.identify_needed`` before anything is spent. Keyed by REASON, not by
# verdict: ``partial_identity_owed_full_read`` yields the same ``rfid_refresh`` string but
# is event-shaped (the push itself is the new evidence) and passes straight through.
_NEED_GATED_REASONS = frozenset(
    {
        "identity_unresolved",
        "spent_occupied_owed_identify",
        "identity_ambiguous_owed_full_read",
    }
)

# One lock per printer: a pass reads the slot's binding, decides, and writes, and two
# concurrent pushes interleaving those steps is how a slot gets two assignment rows —
# both read "no assignment for (printer, ams, tray)" and both INSERT, hitting the
# spool_assignment_printer_id_ams_id_tray_id_key unique constraint (surfaced on Postgres;
# SQLite's WAL serial writes hid it). MQTT bursts deliver two AMS pushes ~30 ms apart on
# H2D + dual AMS, so this is not theoretical. Process-lifetime, keyed by printer id —
# passes for DIFFERENT printers stay fully concurrent.
_pipeline_locks: dict[int, asyncio.Lock] = {}

# (spool_id, scanned tag) pairs already WARNed about as a third chip. Not the sibling
# dedup — that is the ``spool.sibling_tag_uid`` column now (see :func:`_record_sibling_tag`);
# this holds only physically-impossible reads, of which a healthy fleet has none. It
# exists because the condition STANDS and re-derives on every push, and a 1 Hz warning
# buries the fact it is reporting.
_chimera_warned: set[tuple[int, str]] = set()

# Per-printer dedup for ``unknown_tag`` prompts: {printer_id: {(ams, tray): (tag, uuid)}}.
# Re-broadcast only when the tag tuple CHANGES for a slot; cleared when the slot reports
# empty or gets bound, so remove + reinsert reliably re-prompts. Moved here from
# ``main.py`` at the W3b cutover: the prompt is now raised by exactly one decision
# (``unknown_tag_prompt_owed``) and cleared by exactly two pipeline outcomes (a released
# / empty slot and a successful bind), so its state belongs beside them. The Spoolman
# lane in ``main.on_ams_change`` imports the two public helpers — one origin, one dedup
# ledger, whichever mode raised the prompt.
_unknown_tag_last_broadcast: dict[int, dict[tuple[int, int], tuple[str, str]]] = {}


def _reset_state() -> None:
    """Test hook: clear module-level locks + dedup state between cases."""
    _pipeline_locks.clear()
    _chimera_warned.clear()
    _unknown_tag_last_broadcast.clear()


# --- injectables ------------------------------------------------------------

SettingReader = Callable[[str], Awaitable[str | None]]
IdentifyScheduler = Callable[[int, int, int, str], Awaitable[Any]]
Broadcaster = Callable[[dict], Awaitable[Any]]
ConfigPusher = Callable[[Spool, int, int, int, dict], Awaitable[Any]]


@dataclass
class PipelineDeps:
    """Everything the pass needs from outside itself, injectable for tests.

    ``db`` and ``client`` are the two live handles (the printer's
    :class:`BambuMQTTClient`, or None when it is disconnected — every read of it is
    guarded, and an unreadable client resolves to "no wire-safety objection", matching
    ``ams_presence.unit_drying``'s never-raise contract).

    The four hooks default to the production implementations; a test passes fakes and
    the pass performs no wire I/O at all.
    """

    db: AsyncSession
    client: Any = None
    get_setting: SettingReader | None = None
    schedule_identify: IdentifyScheduler | None = None
    broadcast: Broadcaster | None = None
    push_config: ConfigPusher | None = None

    async def setting(self, key: str) -> str | None:
        if self.get_setting is not None:
            return await self.get_setting(key)
        from backend.app.api.routes.settings import get_setting

        return await get_setting(self.db, key)

    async def emit(self, payload: dict) -> None:
        """Broadcast a slot event. Guarded: a websocket failure never fails a write
        that already landed."""
        try:
            if self.broadcast is not None:
                await self.broadcast(payload)
            else:
                await ws_manager.broadcast(payload)
        except Exception:  # noqa: BLE001 — a broadcast failure must not unwind the pass
            logger.exception("[slot-state] broadcast failed for %s", payload.get("type"))

    async def identify(self, printer_id: int, ams_id: int, tray_id: int, reason: str, tray: dict) -> None:
        """Ask the discovery lane for ONE read on this slot — if one is actually owed.

        NEED-driven, never cadence-driven. ``command_identify`` re-checks WIRE SAFETY
        (printer state, engaged extruder, drying, the client's own refusal), but wire
        safety is not need: a standing-unknown slot passes every one of those checks and
        would be read on every push forever. :meth:`identify_verdict` is what decides
        whether this decision has EARNED a read at all (doctrine rule 3 / invariant 4).

        ``tray`` is this push's raw tray dict — the evidence ``ams_presence.identify_needed``
        judges. It is used ONLY by the production default; an injected
        ``schedule_identify`` owns its own policy and keeps the 4-argument hook signature.
        """
        try:
            if self.schedule_identify is not None:
                await self.schedule_identify(printer_id, ams_id, tray_id, reason)
                return
            verdict = await self.identify_verdict(printer_id, ams_id, tray_id, reason, tray)
            if verdict is None:
                return
            await ams_presence.command_identify(
                printer_id,
                ams_id,
                tray_id,
                source="reconcile",
                reason=verdict,
            )
        except Exception:  # noqa: BLE001 — an identify failure must not unwind the pass
            logger.exception(
                "[slot-state] identify scheduling failed for printer %d A%dT%d", printer_id, ams_id, tray_id
            )

    async def identify_verdict(self, printer_id: int, ams_id: int, tray_id: int, reason: str, tray: dict) -> str | None:
        """The ``ams_presence`` reason this decision has earned, or None to spend nothing.

        Two shapes (see :data:`_IDENTIFY_VERDICT` and :data:`_NEED_GATED_REASONS`):

        * EVENT-SHAPED — ``partial_identity_owed_full_read``. A half identity WAS
          asserted over a bound slot this very push, so the read is both owed and
          answerable; the evidence is the push itself and re-deriving it would only cost
          a query.
        * STANDING — a REQUEST, never an entitlement: a standing unknown, a spent latch
          under a seated unnamed roll, or a tag disagreement the wire keeps re-asserting.
          ``identify_needed`` is the fork's one need authority (it owns the
          unanswered-qualified-cycle test AND the episode occasions), so it answers here,
          and a ``None`` verdict means the condition is merely STANDING — already read,
          or never a reason to read — which is not a reason to read it again. Whatever
          reason it DOES return is the one passed on, so a slot that turns out to be
          live-tagged gets the accurate ``rfid_refresh`` rather than this decision's
          guess.

        ``spoolman_active=False`` is a fact, not an assumption: :func:`run_slot_pipeline`
        stands the whole pass down under Spoolman mode, so no Spoolman binding can reach
        this call.

        An unmapped reason returns None — fail-CLOSED, because the only cost of not
        reading is a slot whose identity resolves one push later.
        """
        verdict = _IDENTIFY_VERDICT.get(reason)
        if verdict is None or reason not in _NEED_GATED_REASONS:
            return verdict
        need = await ams_presence.identify_needed(self.db, printer_id, ams_id, tray_id, tray, False)
        if need is None:
            logger.debug(
                "[slot-state] printer=%d A%dT%d no identify need — standing unknown is not a read reason",
                printer_id,
                ams_id,
                tray_id,
            )
            return None
        return need

    async def push_slot_config(self, spool: Spool, printer_id: int, ams_id: int, tray_id: int, tray: dict) -> bool:
        """Publish a slot's filament identity through the ONE tagless config funnel
        (which carries its own settle gate — see ``spool_tagless._push_config``)."""
        try:
            if self.push_config is not None:
                return bool(await self.push_config(spool, printer_id, ams_id, tray_id, tray))
            return bool(await spool_tagless.push_config_for_spool(self.db, spool, printer_id, ams_id, tray_id, tray))
        except Exception:  # noqa: BLE001 — a refused/failed push must not unwind the pass
            logger.exception(
                "[slot-state] config push failed for spool %d on printer %d A%dT%d",
                getattr(spool, "id", -1),
                printer_id,
                ams_id,
                tray_id,
            )
            return False


@dataclass(frozen=True)
class AppliedTransition:
    """One slot's outcome for one pass — the pass's machine-readable record.

    ``applied`` is True only when the binding ledger actually CHANGED: a KEEP, a DEFER,
    a damped bind and a skipped duplicate all report False while still appearing in the
    list, so a caller (and a test) can see every slot the pass considered.

    ``to_state`` rides alongside ``from_state`` because the audit line needs both ends
    of the transition; ``from_state`` is the state the DURABLE facts implied before this
    push (:func:`_believed_state`) and ``to_state`` is what the slot is in AFTER the
    decision was applied (:func:`slot_state.post_state`) — or, when nothing was applied,
    the state this push derives.
    """

    slot: tuple[int, int, int]
    from_state: SlotState
    to_state: SlotState
    decision: Decision
    applied: bool


# --- entry point ------------------------------------------------------------


def _pipeline_lock(printer_id: int) -> asyncio.Lock:
    """The per-printer pass lock, created on first use."""
    lock = _pipeline_locks.get(printer_id)
    if lock is None:
        lock = asyncio.Lock()
        _pipeline_locks[printer_id] = lock
    return lock


async def run_slot_pipeline(
    printer_id: int, observations: list[TrayObservation], deps: PipelineDeps
) -> list[AppliedTransition]:
    """Process ONE push's observations for ONE printer. Never raises.

    Serialized per printer (see :func:`_pipeline_lock`). Slots are processed in payload
    order; a slot that fails logs ERROR and is skipped so the rest of the pass still
    runs. Returns one :class:`AppliedTransition` per slot considered.
    """
    transitions: list[AppliedTransition] = []
    # Slots per AMS unit in THIS push — the unknown-tag prompt payload carries it so the
    # kiosk can lay out the unit. Derived from the observations rather than threaded from
    # the wire dict, because the observations ARE this push's view of the unit.
    # Read defensively (``getattr``, not ``obs.ams_id``): this runs OUTSIDE the per-slot
    # guard below, so one malformed entry must not cost the whole pass — invariant 10.
    tray_counts: dict[int, int] = {}
    for obs in observations:
        ams_id = getattr(obs, "ams_id", None)
        if isinstance(ams_id, int):
            tray_counts[ams_id] = tray_counts.get(ams_id, 0) + 1
    try:
        if await _spoolman_owns_slots(deps):
            # Spoolman mode owns AMS slots end-to-end (its own sync lane in
            # ``main.on_ams_change``, and ``settings.py``'s mode switch wipes the
            # internal assignments outright). The pipeline stands down entirely rather
            # than deciding identity for rows it does not own.
            return transitions
        async with _pipeline_lock(printer_id):
            seen: set[int] = set()
            for obs in observations:
                try:
                    transition = await _process_observation(obs, deps, seen, tray_counts)
                except Exception:  # noqa: BLE001 — one bad slot must never abort the pass
                    logger.exception(
                        "[slot-state] slot processing failed for printer %s slot %s",
                        printer_id,
                        getattr(obs, "slot", "?"),
                    )
                    await _rollback(deps)
                    continue
                if transition is not None:
                    transitions.append(transition)
    except Exception:  # noqa: BLE001 — invariant 10: nothing escapes into the callback
        logger.exception("[slot-state] pipeline pass failed for printer %s", printer_id)
    return transitions


async def _spoolman_owns_slots(deps: PipelineDeps) -> bool:
    """Is this install in Spoolman mode? Unreadable setting = internal mode (run).

    Fail-OPEN on purpose: the pipeline is the only thing keeping bindings wire-true in
    the fork's default mode, and silently standing down because one setting read failed
    would strand every slot. A genuine Spoolman install answers this read reliably.
    """
    try:
        raw = await deps.setting("spoolman_enabled")
    except Exception:  # noqa: BLE001 — an unreadable setting is not evidence of Spoolman
        logger.exception("[slot-state] spoolman_enabled lookup failed — running the pass")
        return False
    return bool(raw) and raw.lower() == "true"


async def _rollback(deps: PipelineDeps) -> None:
    try:
        await deps.db.rollback()
    except Exception:  # noqa: BLE001 — a failed rollback is already the worst case
        logger.exception("[slot-state] rollback failed")


# --- one slot ---------------------------------------------------------------


async def _process_observation(
    obs: TrayObservation, deps: PipelineDeps, seen: set[int], tray_counts: dict[int, int] | None = None
) -> AppliedTransition | None:
    printer_id, ams_id, tray_id = obs.slot

    if obs.present is False:
        # An emptied slot invalidates any prompt raised for the roll that just left, so
        # reinserting one re-prompts. Kept here rather than in a decision branch because
        # it must happen for a BOUND slot (which releases) and an UNBOUND one (which
        # decides NONE) alike. Sibling clears for the respool prompt and the bare-tray
        # auto-config window live in ``main.on_ams_change``'s consumer loop — those are
        # consumer-lane state, not identity.
        clear_unknown_tag_dedup(printer_id, ams_id, tray_id)

    assignment = await _load_assignment(deps.db, printer_id, ams_id, tray_id)
    if assignment is not None and assignment.spool is None:
        # Orphan: the assignment outlived its spool row (hand delete / cascade race).
        # The location claim is bogus either way — drop it through the ONE unbind
        # writer so it still leaves the structured release line.
        return await _release_orphan(obs, deps, assignment)

    binding = _binding_view(assignment)
    ctx = await _build_context(obs, deps, binding)

    state = derive_state(obs, binding)
    decision = resolve(obs, state, ctx)
    from_state = _believed_state(binding)

    decision, applied = await _apply(obs, deps, assignment, decision, seen, tray_counts or {})
    await _settle_recheck_intent(obs, deps, decision, applied, ctx)

    # The RIGHT side of the transition is what the APPLIED decision leaves behind, not
    # ``state`` — that is ``derive_state`` against the PRE-transition binding, so logging
    # it printed tautologies (prod: ``SPENT_AWAITING_SWAP→SPENT_AWAITING_SWAP
    # replace_spent``). When nothing was applied — a KEEP, a DEFER, a damped bind — the
    # binding did not move and the derived classification is still the honest after-state.
    to_state = post_state(decision, state) if applied else state

    if applied:
        logger.info("%s", format_slot_event(printer_id, ams_id, tray_id, from_state, to_state, decision))
    return AppliedTransition(
        slot=obs.slot, from_state=from_state, to_state=to_state, decision=decision, applied=applied
    )


#: Decision kinds that ANSWER an open re-check by re-deciding the slot through some other
#: lane. A tag landed and the identity lane bound it (scenario R4), the roll was pulled, a
#: fresh row was minted, a spent swap completed — in every case the question "what is in this
#: slot?" now has an answer, so leaving the intent open would make the farm go on owing a
#: read for a slot it has just resolved.
#:
#: KEEP / DEFER / NONE are excluded because the slot is still unresolved and the operator is
#: still waiting. **RECLAIM is excluded too, and that one is not obvious.** The de-bounce
#: concludes the exact OPPOSITE of the click: it says the release was spurious and the same
#: roll never left, while the operator has just said something moved. It is also
#: assumption-tier evidence (invariant 11) against a human answer, which rule 6 names an
#: identity ORACLE. Letting it settle the intent would silently swallow the answer and hand
#: the slot back to the very row the operator was correcting — so the intent stays open, the
#: owed read still lands, and row 3½ then replaces the de-bounced row.
_RECHECK_SETTLING_KINDS = frozenset(
    {DecisionKind.BIND, DecisionKind.MINT, DecisionKind.RELEASE, DecisionKind.REPLACE_SPENT}
)


async def _settle_recheck_intent(
    obs: TrayObservation, deps: PipelineDeps, decision: Decision, applied: bool, ctx: ResolutionContext
) -> None:
    """Close the slot's open re-check intent, or ask for the read it is still waiting on.

    The intent's whole lifecycle in one place, run after the decision has been applied so it
    reads the OUTCOME rather than guessing at it (the same SRP lesson
    :func:`_settle_physical_cycle` records for the physical cycle).

    * The re-check's OWN verdicts resolve it carrying the minted row — the one fact that
      scopes the acknowledgement's undo to mints the operator caused.
    * Any other applied re-decision resolves it carrying nothing: the question was answered
      by a better oracle (scenario R4 is exactly this — a tag landed, row 2 decided).
    * Otherwise the slot is still unresolved, so ASK. Not need-gated, because the click IS
      the need and ``identify_needed`` answers None for an ordinary bound tagless slot; paced
      and wire-safe inside ``slot_recheck.maybe_ask``.

    Fully guarded: this is a satellite of the identity pass and may never cost it a slot
    (cross-cutting invariant 10).
    """
    printer_id, ams_id, tray_id = obs.slot
    try:
        if not await slot_recheck.has_open_intent(deps.db, printer_id, ams_id, tray_id):
            # The common case by far: no question is outstanding for this slot, and the
            # memory index answers it without a query.
            return
        if applied and decision.reason in _RECHECK_REASONS:
            # The MINTED row's id, read back from the slot rather than from the decision:
            # a MINT's ``spool_id`` is None (the row did not exist when the table decided)
            # and a REPLACE_SPENT's names the row that just RETIRED. The binding is the
            # only honest answer to "what did this click create?".
            minted_id = (
                await deps.db.execute(
                    select(SpoolAssignment.spool_id).where(
                        SpoolAssignment.printer_id == printer_id,
                        SpoolAssignment.ams_id == ams_id,
                        SpoolAssignment.tray_id == tray_id,
                    )
                )
            ).scalar_one_or_none()
            await slot_recheck.resolve_slot(deps.db, printer_id, ams_id, tray_id, minted_spool_id=minted_id)
            return
        if applied and decision.kind in _RECHECK_SETTLING_KINDS:
            await slot_recheck.resolve_slot(deps.db, printer_id, ams_id, tray_id)
            return
        await slot_recheck.maybe_ask(deps.db, printer_id, ams_id, tray_id, busy=ctx.busy, seated=obs.present is True)
    except Exception:  # noqa: BLE001 — invariant 10: nothing escapes into the callback chain
        logger.exception("[slot-state] re-check intent settle failed for printer %d A%dT%d", *obs.slot)


async def _load_assignment(db: AsyncSession, printer_id: int, ams_id: int, tray_id: int) -> SpoolAssignment | None:
    res = await db.execute(
        select(SpoolAssignment)
        .options(selectinload(SpoolAssignment.spool))
        .where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    return res.scalar_one_or_none()


async def _release_orphan(obs: TrayObservation, deps: PipelineDeps, assignment: SpoolAssignment) -> AppliedTransition:
    printer_id, ams_id, tray_id = obs.slot
    await release_spool_from_slot(deps.db, assignment, reason=ORPHAN_RELEASE_REASON)
    await deps.db.commit()
    decision = Decision(DecisionKind.RELEASE, spool_id=assignment.spool_id, reason=ORPHAN_RELEASE_REASON)
    logger.info(
        "%s", format_slot_event(printer_id, ams_id, tray_id, SlotState.OCCUPIED_ASSUMED, SlotState.EMPTY, decision)
    )
    await _emit_assignment_changed(obs, deps)
    return AppliedTransition(
        slot=obs.slot,
        from_state=SlotState.OCCUPIED_ASSUMED,
        to_state=SlotState.EMPTY,
        decision=decision,
        applied=True,
    )


# --- views ------------------------------------------------------------------


def _binding_view(assignment: SpoolAssignment | None) -> BindingView | None:
    """The slot's current binding as the table sees it.

    The fingerprint comes from the ASSIGNMENT, never the spool: the assignment's
    snapshot is what "the filament this binding stands for" means (and what phase-1
    compared), while the spool's material/rgba is the roll's own identity and may have
    been edited by an operator.

    ``spent`` reads ``Spool.is_finished_roll`` rather than re-spelling its ``spent_at``
    test: the table's two G3 conclusions are BUILT on that predicate meaning one thing
    (see the model), and a mirror is only a mirror while it goes through the original.
    """
    if assignment is None or assignment.spool is None:
        return None
    spool = assignment.spool
    return BindingView(
        spool_id=spool.id,
        is_tagless=is_tagless_spool(spool),
        tag_uid=spool.tag_uid,
        sibling_tag_uid=spool.sibling_tag_uid,
        tray_uuid=spool.tray_uuid,
        spent=spool.is_finished_roll,
        archived=spool.archived_at is not None,
        fingerprint_type=assignment.fingerprint_type,
        fingerprint_color=assignment.fingerprint_color,
        pre_configured=assignment.pre_configured_at is not None,
    )


def _spool_view(spool: Spool) -> SpoolView:
    """A CANDIDATE row's view: the fingerprint is the spool's own material/rgba,
    because an unbound row has no assignment snapshot to speak for it."""
    return BindingView(
        spool_id=spool.id,
        is_tagless=is_tagless_spool(spool),
        tag_uid=spool.tag_uid,
        sibling_tag_uid=spool.sibling_tag_uid,
        tray_uuid=spool.tray_uuid,
        spent=spool.is_finished_roll,
        archived=spool.archived_at is not None,
        fingerprint_type=spool.material,
        fingerprint_color=spool.rgba,
        pre_configured=False,
    )


def _believed_state(binding: BindingView | None) -> SlotState:
    """What the DURABLE facts alone said this slot was, before this push.

    Not stored anywhere (the state machine is derived by design — plan §"Schema — NO
    NEW TABLES"): it is re-derived from the binding row + spool flags, which is exactly
    what the previous push's answer was built from. Used only as the LEFT side of the
    audit line, so an operator reading ``OCCUPIED_ASSUMED→EMPTY release`` sees the
    change rather than a tautology.
    """
    if binding is None:
        return SlotState.EMPTY
    if binding.spent:
        return SlotState.SPENT_AWAITING_SWAP
    if binding.pre_configured:
        return SlotState.PRE_CONFIGURED
    if binding.tag_uid or binding.tray_uuid:
        return SlotState.OCCUPIED_IDENTIFIED
    return SlotState.OCCUPIED_ASSUMED


# --- candidates -------------------------------------------------------------


async def _identity_candidate(db: AsyncSession, obs: TrayObservation) -> Spool | None:
    """The row that OWNS this push's identity — uuid-primary, EXACT matches only.

    ``tray_uuid`` names the roll (two chips, one uuid — 4/4 prod slots 2026-08-01), so
    the uuid owner answers first; a bare tag falls back to strict equality against
    EITHER chip the row carries. Deliberately NOT ``get_spool_by_tag``: its
    suffix/first-char variance lanes exist for legacy callers, and a widened row handed
    to the table as a certainty is the false-merge hazard (plan §"Root causes
    confirmed"). The table re-checks whatever arrives against the observation anyway, so
    a widened row could not be smuggled in — this just never produces one.

    Matching ``sibling_tag_uid`` is exactness, not widening: the pair is the roll's
    recorded identity, both members written from real reads of that physical roll.
    Without it a push carrying only the far-side chip (no uuid — the incremental-push
    shape) finds NO owner, and the roll's own row cannot answer for it.
    """
    if obs.tray_uuid:
        spool = await find_spool_sharing_tray_uuid(db, obs.tray_uuid)
        if spool is not None:
            return spool
    if obs.tag_uid:
        scanned = obs.tag_uid.upper()
        res = await db.execute(
            select(Spool)
            .options(selectinload(Spool.k_profiles), selectinload(Spool.assignments))
            .where(
                or_(func.upper(Spool.tag_uid) == scanned, func.upper(Spool.sibling_tag_uid) == scanned),
                Spool.archived_at.is_(None),
            )
            .limit(1)
        )
        return res.scalar_one_or_none()
    return None


async def _untagged_claim_candidate(
    db: AsyncSession, obs: TrayObservation, binding: BindingView | None
) -> Spool | None:
    """The operator row this newly-identified roll plausibly IS. Strict priority.

    Only consulted when the push asserted an identity AND no row already OWNS it
    (:func:`_identity_candidate` came back empty), so this costs one extra query in the
    worst case and none on the common path.

    1. **The slot's own pre-configured binding**, when its spool carries no tag/uuid.
       SpoolBuddy weigh-then-assign binds a WEIGHED row to an empty slot; the tag the
       AMS reads when that roll is inserted belongs to that row. Resolved from the
       binding already loaded for this slot — no query at all. It has to be a separate
       priority because :func:`find_matching_untagged_spool` excludes every row that
       holds an assignment (``~assignments.any()``), which is exactly what this row is.
    2. **The attract lane** — ``find_matching_untagged_spool``: an unassigned, untagged,
       non-archived, Bambu-or-unset-brand, material+colour-matching operator row, never
       an auto-minted ``ams_auto`` one. Its criteria are the contract here; this calls it
       rather than restating them, so "a row that can attract a tag" keeps one meaning.
    """
    if binding is not None and binding.pre_configured and not (binding.tag_uid or binding.tray_uuid):
        spool = await db.get(Spool, binding.spool_id)
        # Re-read the ROW rather than trusting the view: the view's identity fields are a
        # snapshot, and linking a tag onto a row that already carries one would be the
        # duplicate-identity hazard this whole lane exists to avoid.
        if spool is not None and spool.archived_at is None and is_tagless_spool(spool):
            return spool
    return await find_matching_untagged_spool(db, observation_tray_dict(obs))


def _fingerprint_compatible(obs: TrayObservation, spool: Spool) -> bool:
    """Same physical filament as this push, judged on the SPOOL's own identity.

    Same two one-origin helpers the table uses (``colors_similar`` +
    ``canonical_filament_type``), so "same filament" means one thing fork-wide.
    """
    if not colors_similar(obs.tray_color or "", spool.rgba or ""):
        return False
    return canonical_filament_type(obs.tray_type or "") == canonical_filament_type(spool.material or "")


async def _debounce_candidate(db: AsyncSession, obs: TrayObservation) -> Spool | None:
    """The DE-BOUNCE donor: the one roll that last left THIS slot, iff it may stand for a
    spurious release.

    This lane used to be called the "reclaim", and the rename is the point. It was written
    as an identity claim — "a pulled roll came back to where it was" — and on this fleet it
    could not be one: every production tray reports ``tag_uid: null``, ``tray_uuid: null``,
    ``remain: -1``, so :func:`_fingerprint_compatible` (material + colour) is ALWAYS TRUE,
    against ``last_location_*`` residue that nothing ever clears and no age bounds. It hung
    spool 292 (954.4 g used) and spool 298 (936.1 g) onto brand-new rolls after 997 and 804
    minutes, and every queued unit staged "Low filament" on full filament (shape 32).

    What the 8-day gap distribution showed is that the lane was really doing a DIFFERENT,
    load-bearing job: of 52 reclaims with a matched prior release, **14 landed under five
    minutes** — four at 0.0 min across four printers inside one minute — which no human
    performs. Those are SPURIOUS releases (one glitched exist bit hard-deletes a binding
    since wave 2), and the lane was silently repairing them. Deleting it outright would
    reset a part-used roll to label weight several times a day and walk it straight through
    the ``min_start_spool_g`` gate. So the lane survives, SCOPED to what it can actually
    prove: a glitch filter, never an identity oracle (doctrine rule 7 as amended
    2026-08-19, and its amendment block).

    THIS function owns the two SPOOL-side conditions; the two wire-side ones
    (``reseat_within_window``, ``runout_suspect``) are computed in :func:`_build_context`
    and weighed by the table, so ``slot_state.resolve`` stays a pure function of its inputs.

    * **The slot's single last occupant — adjudicated, never scanned past** (``.limit(1)``).
      The old query scanned up to 25 released rows and took the first fingerprint match,
      which is a search through noise; there is exactly ONE candidate a spurious release can
      be about (operator ruling 15). This is the same discipline
      ``spool_respool._mark_tray_spent``'s tier 2 already applies to the same residue: walk
      past the newest row and you stamp an OLDER, healthy one — permanently.
    * **The donor is UNTAGGED** (``spool_tagless.is_tagless_spool``, all three identity
      columns). A tagged roll re-asserts its own identity the moment the AMS reads it, so a
      breadcrumb can only ever PRE-EMPT that read — which is exactly what drove spool 250 to
      1246 g on a 1000 g label and made the ledger-overcharge lane warn twice that it could
      not reconcile a tag-bound row. Doctrine rule 11: an assumption may not speak for a roll
      that can speak for itself.

    Archived, spent and bound-elsewhere rows are all REFUSED — but they are refused in
    PYTHON, on the one row the query returned, and never filtered out in SQL. That
    distinction is the whole of the "never scanned past" clause and it is not a style
    preference: a ``WHERE`` clause does not skip an ineligible row, it makes it INVISIBLE,
    so the query silently answers with the newest row that happens to PASS — an OLDER
    residue of the same slot, a different physical roll. The shipped filters did exactly
    that: a just-drained roll (released inside the ~3-minute bay-clear→HMS gap, so not yet
    spent... and then spent moments later) was stepped over onto a healthy shelf roll that
    once sat in the same tray, and THAT row was reclaimed onto the brand-new one. Nothing
    could heal it afterwards, because the reclaimed row never runs out.

    So each refusal is now a stated conclusion with its own log line:

    * ARCHIVED — retired inventory may not take a slot;
    * SPENT — the W1 latch owns a drained row, which is why a runout whose stamp has
      already landed MINTS (scenario T6);
    * BOUND ELSEWHERE — assumption-tier evidence may displace nothing a live binding holds
      (invariant 11; the spool-211 ping-pong, shape 26). A slot MOVE stamps the OLD slot's
      breadcrumb, so this is an ordinary shape, not an exotic one.

    With no surviving donor, None is the doctrine-correct answer and the table mints.
    """
    printer_id, ams_id, tray_id = obs.slot
    res = await db.execute(
        spool_binding.last_released_from_slot_stmt(printer_id, ams_id, tray_id)
        .options(selectinload(Spool.k_profiles))
        .limit(1)
    )
    donor = res.scalars().first()
    if donor is None:
        return None
    # Adjudicate the ONE row; never fall through to an older one (operator ruling 15).
    refusal: str | None = None
    if donor.archived_at is not None:
        refusal = "ARCHIVED (retired inventory never takes a slot)"
    elif donor.spent_at is not None:
        refusal = "SPENT (the drained roll belongs to the W1 latch — scenario T6)"
    elif spool_binding.bound_elsewhere(donor):
        refusal = "BOUND ELSEWHERE (invariant 11 — a breadcrumb displaces no live binding)"
    elif not is_tagless_spool(donor):
        refusal = "TAGGED (rule 11 — its own tag outranks a breadcrumb)"
    if refusal is not None:
        logger.info(
            "[slot-state] printer=%d A%dT%d de-bounce refused: this slot's last occupant, spool %d, is %s "
            "— minting rather than reaching past it to an older residue",
            printer_id,
            ams_id,
            tray_id,
            donor.id,
            refusal,
        )
        return None
    if not _fingerprint_compatible(obs, donor):
        return None
    return donor


def _live_hms(deps: PipelineDeps) -> list:
    """The printer's live ``state.hms_errors`` list, or empty. Never raises.

    Guarded on its own for the same reason :func:`_mask_facts` is: a disconnecting client
    is missing EVIDENCE, and missing evidence must read as "nothing standing", never as an
    exception on the AMS callback path (invariant 10).
    """
    try:
        return list(getattr(getattr(deps.client, "state", None), "hms_errors", None) or [])
    except Exception:  # noqa: BLE001 — an unreadable client asserts nothing
        return []


def _runout_suspect(obs: TrayObservation, deps: PipelineDeps, incident: PrinterIncident | None) -> bool:
    """Does this slot's current occupancy follow a RUNOUT rather than a glitch?

    Operator ruling 15's second clause — **a runout release is never a glitch** — and the
    reason the de-bounce is disqualified BY CAUSE before its window is ever consulted
    (doctrine rule 6's "by cause, never by duration", applied to a decision rather than a
    read).

    Without this the wave would have made things WORSE for the case it most needed to fix.
    The AMS clears a drained slot's exist bit **~3 minutes BEFORE** it declares the runout
    (shape 31, three timed pairs): inside that gap the departed row is released but not yet
    spent, so the ``spent_at`` filter cannot see it, and the de-bounce would bind the
    EXHAUSTED row to the fresh roll the operator just loaded — after which the runout's own
    HMS stamps ``spent_at`` on it and the slot reads ~950 g used / 0 g remaining. Scoping
    the lane to short gaps CONCENTRATES that case, because a refill on a slot the AMS is
    demanding is precisely a fast return (scenarios T7/T8).

    THREE evidences, any one sufficient, each covering what the others structurally cannot.
    All three are matched on THIS EXACT SLOT, never blanket-per-printer: a genuine glitch on
    slot 2 while slot 3 is held for a runout must still de-bounce, and over-suspecting mints
    a part-used roll back to label weight, which is the unsafe direction.

    * **Inside the gap** — the slot was the ACTIVE FEEDER of a live print when its presence
      was lost (``ams_presence.reseat_under_active_feed``, the gain-side readback of the
      loss-edge stamp ``spool_recovery.slot_was_feeding`` wrote). A slot that loses presence
      while it is feeding is running out or being pulled mid-print; neither is a glitch.
      Blind when a firmware auto-refill moved the feed to a backup BEFORE the bit cleared,
      and erased entirely by a restart inside the gap.
    * **On the wire, right now** — the firmware is standing on a slot-attributed runout for
      this slot (``hms_errors.runout_standing_for_slot`` over the live HMS list). This is
      the arm that covers a CASCADE's second slot and a printer whose one open incident is
      a JAM, because an incident row is bounded to ONE OPEN PER PRINTER by design
      (``printer_incident``'s partial unique index) while the wire keeps naming every dry
      slot independently. It also survives what the loss edge cannot: it is re-read from the
      live push on every pass, so a restart re-derives it for free.
    * **After the HMS** — an OPEN ``runout`` incident naming this exact slot. Kept beside
      the wire arm rather than replaced by it: the incident is the farm's DURABLE record of
      a hold, and a firmware that clears its HMS while the print is still held (a demand
      that goes CLEAR while PAUSEd — the refill-done evidence the resume lane watches for)
      would otherwise take the suspicion away at exactly the moment the operator is
      standing at the printer with a fresh roll.

    Still not exhaustive, and the SECOND layer is what makes that acceptable: the physical
    cycle survives an unbound resolution, so a runout whose spent stamp lands after a
    de-bounce drives ``REPLACE_SPENT`` on the very next push (scenarios T8b/T8c). Prevention
    where the cause is visible; repair where it is not.
    """
    printer_id, ams_id, tray_id = obs.slot
    if ams_presence.reseat_under_active_feed(printer_id, ams_id, tray_id):
        return True
    try:
        if hms_errors.runout_standing_for_slot(_live_hms(deps), ams_id, tray_id):
            return True
    except Exception:  # noqa: BLE001 — an undecodable HMS list is no evidence, never a crash
        logger.exception("[slot-state] live runout decode failed for printer %d A%dT%d", printer_id, ams_id, tray_id)
    if incident is None or incident.kind != KIND_RUNOUT:
        return False
    # Through the fork's ONE global-tray codec (invariant 1): the bare ``ams_id * 4``
    # arithmetic that used to sit here is wrong for an AMS-HT unit (``global == ams_id``)
    # and for the external holder (254/255), so on those topologies it compared the
    # incident's honest global id against a fabricated one and answered False forever.
    return incident.slot_global_tray == encode_global_tray(ams_id, tray_id)


async def _build_context(obs: TrayObservation, deps: PipelineDeps, binding: BindingView | None) -> ResolutionContext:
    printer_id, ams_id, tray_id = obs.slot

    # Row 1's inputs FIRST. All three are cheap, synchronous, in-memory reads, and the
    # table returns DEFER on any of them before it looks at a single other field
    # (``slot_state.resolve``: drying → identify_in_flight → settling, ahead of even a
    # perfect tag match). Evaluating them here is a pure reorder — the ResolutionContext
    # this builds is identical either way — but it lets the de-bounce lane's DB work be
    # skipped for a slot whose decision is already made. A drying unit or a settling
    # insertion is the steady state for minutes at a time on a busy printer, at ~1 Hz per
    # slot, so this is a query per push per slot that could never influence anything.
    drying = _drying(deps, ams_id)
    identify_in_flight = _identify_in_flight(deps, ams_id)
    settling = _settling(printer_id, ams_id, tray_id)
    deferring = drying or identify_in_flight or settling

    identity_candidate = None
    untagged_claim_candidate = None
    if obs.identity_asserted:
        candidate = await _identity_candidate(deps.db, obs)
        if candidate is not None:
            identity_candidate = _spool_view(candidate)
        else:
            # No row owns this identity — before the table can mint a stranger, ask
            # whether an operator already logged this roll (weighed pre-config row, or
            # an untagged inventory row awaiting its tag).
            claim = await _untagged_claim_candidate(deps.db, obs, binding)
            if claim is not None:
                untagged_claim_candidate = _spool_view(claim)

    # The de-bounce lane's three inputs, all resolved HERE so ``slot_state.resolve`` stays a
    # pure function of its arguments (the same shape ``qualified_cycle_pending`` uses). The
    # wire-side predicates are only read when a donor actually exists, so an ordinary slot
    # costs no extra work.
    debounce_candidate = None
    reseat_within_window = False
    runout_suspect = False
    if not deferring and binding is None and obs.config_nonempty and not obs.identity_asserted:
        donor = await _debounce_candidate(deps.db, obs)
        if donor is not None:
            debounce_candidate = _spool_view(donor)
            reseat_within_window = ams_presence.reseat_within_window(printer_id, ams_id, tray_id)
            runout_suspect = _runout_suspect(obs, deps, await printer_incidents.get_open(deps.db, printer_id))

    return ResolutionContext(
        binding=binding,
        identity_candidate=identity_candidate,
        untagged_claim_candidate=untagged_claim_candidate,
        debounce_candidate=debounce_candidate,
        reseat_within_window=reseat_within_window,
        runout_suspect=runout_suspect,
        qualified_cycle_pending=spool_tagless.qualified_cycle_pending(printer_id, ams_id, tray_id),
        no_tag_read_answered=_no_tag_read_answered(obs, binding),
        operator_recheck_answered=await _operator_recheck_answered(obs, deps, binding),
        auto_add_unknown=await _auto_add_unknown(deps),
        busy=_printer_busy(deps),
        settling=settling,
        identify_in_flight=identify_in_flight,
        drying=drying,
        tagless_default=await _tagless_default(deps),
    )


async def _operator_recheck_answered(obs: TrayObservation, deps: PipelineDeps, binding: BindingView | None) -> bool:
    """Has the operator's re-check of this slot got its ANSWER? (doctrine rule 12, WS11)

    Both halves, resolved here so ``slot_state.resolve`` stays pure — the same shape
    ``qualified_cycle_pending`` and the de-bounce's two predicates already use:

    * a DURABLE open intent for this slot (``slot_recheck.has_open_intent`` — a process-memory
      index over the rows, so this costs no query per push);
    * a commanded discovery read that came back finding NO CHIP
      (``slot_recheck.tag_ness_answered``).

    Ordered cheapest-first, and every early return is a doctrine rule rather than a
    micro-optimisation. Presence must be the tri-state True (invariant 3 — an unknown is never
    resolved toward action, and an answered read needs a seated tray to have been read AT).
    A busy printer can never satisfy this at all: mid-print the farm commands no reads
    (rule 5), so any stamp still standing predates the print and cannot be an answer ABOUT
    the roll inserted during it — believing it is exactly how scenario R4's reused-tag RFID
    roll would be mis-minted as tagless.

    A pre-configured binding is excluded here as well as in the table: operator intent is
    never guessed over (scenario T13), so a slot awaiting a pre-assigned roll owes the
    re-check nothing.
    """
    if obs.present is not True or obs.identity_asserted:
        return False
    if binding is not None and binding.pre_configured:
        return False
    if _printer_busy(deps):
        return False
    try:
        if not await slot_recheck.has_open_intent(deps.db, *obs.slot):
            return False
        return slot_recheck.tag_ness_answered(*obs.slot, seated=True, identity_asserted=obs.identity_asserted)
    except Exception:  # noqa: BLE001 — an unreadable intent is "no answer", never a crash
        logger.exception("[slot-state] re-check intent lookup failed for printer %d A%dT%d", *obs.slot)
        return False


def _no_tag_read_answered(obs: TrayObservation, binding: BindingView | None) -> bool:
    """Did a commanded discovery read on this slot answer NO TAG over a row that CLAIMS one?

    Doctrine rule 11 (operator-ratified 2026-08-19): *a tagless roll can NEVER be an RFID
    roll — in every scenario, not one lane.* Finding no chip over a binding that has one is
    the same certainty class as two disagreeing ``tray_uuid``s, and the certainty does not
    depend on whether the roll ran dry or on how much the tray happened to say about itself.
    Two gates confined it here until this wave and both are gone:

    * **the SPENT gate.** Whether the bound roll is exhausted has nothing to do with whether
      the seated object IS that roll. This is the restriction rule 11 names by name.
    * **the BARE-tray argument.** ``read_answered_no_tag`` takes bare-ness as a
      caller-supplied constraint, and this caller used to hand it the strictest possible
      reading — "this push asserted neither configuration nor identity". A third-party PETG
      roll reports ``tray_type: "PETG"``, ``tray_info_idx: "GFG02"``, ``tag_uid: null``:
      CONFIGURED, not bare, and the commonest physical shape on this fleet — so the evidence
      could never fire for scenario G7 at all. It now hands over the RFID-PAIR reading
      (``not obs.identity_asserted``), the only one that answers the question actually being
      asked — "did the commanded read find a CHIP?" — because configuration is what the farm
      or the firmware wrote INTO the tray, never what the tray said about itself.

      **The two readings were NOT equivalent, and this docstring used to say they were**
      (2026-08-20, capsule A4). ``ams_presence.on_tray_observations``' own
      ``close_answered_read`` call was still passing ``tray_identity_asserted`` — which
      counts ``tray_type`` / ``tray_info_idx`` — so on exactly the G7 shape this arm was
      widened for, the CLOSE lane never fired: read entitlements went unspent,
      ``_physical_cycle_at`` / ``_read_occasion_at`` lingered, and the next ``rfid_refresh``
      on a chipless tray raised an UNSUPPRESSED ``0700_0081`` (invariant 4 — a code that can
      never self-clear on a tagless slot). That lane now passes this same reading, so there
      is ONE bare-ness in the codebase and the claim below is true rather than aspirational.
      ``read_answered_no_tag``'s own contract is untouched: its ``_discovery_read_at`` stamp
      still drives the ``0700_0081`` HMS suppression, and no caller had its meaning changed
      underneath it.

      Row 5a loses nothing by this: it is reached only through rows 5/6, which by
      construction mean "nothing asserted about identity OR configuration", so its
      bare-tray guarantee comes from its POSITION in the table and never from this
      argument.

    Two gates REMAIN, and both are logic rather than caution: ``binding is None`` (nothing
    bound, nothing to contradict) and ``binding.is_tagless`` (a binding that claims no
    identity has nothing to contradict either, and the same bare core reads identically
    before and after a swap — scenario G9, the arm that must not fire). ``is_tagless`` is
    the canonical three-column test (``spool_tagless.is_tagless_spool``, sibling chip
    included) resolved when the view was built; a two-column reading is the exact bug this
    wave already deleted once. The table states the same refusal itself, in
    ``slot_state._no_tag_answer_contradicts`` — that is where the doctrine is decided, this
    is the cheap exit that spares the ledger peek for every slot that can never conclude.

    The TRAY facts are decided HERE, from this push's observation, and handed to
    ``ams_presence`` — the read economy owns the read stamps, the observation lane owns
    presence and assertion (``ams_presence`` must never re-derive either from the merged
    view, which can be a chimera). ``tray_seated`` is the observation's own tri-state
    presence — the canonical rule, which also answers True on a trusted exist bit the
    per-tray ``state`` has not caught up with.
    """
    if binding is None or binding.is_tagless:
        return False
    printer_id, ams_id, tray_id = obs.slot
    try:
        return ams_presence.read_answered_no_tag(
            printer_id,
            ams_id,
            tray_id,
            tray_seated=obs.present is True,
            tray_bare=not obs.identity_asserted,
        )
    except Exception:  # noqa: BLE001 — an unreadable ledger is "no evidence", never a crash
        logger.exception("[slot-state] no-tag read evidence lookup failed for printer %d A%dT%d", *obs.slot)
        return False


async def _auto_add_unknown(deps: PipelineDeps) -> bool:
    """``auto_add_unknown_rfid`` — unset means ON (the pre-cutover default)."""
    raw = await deps.setting("auto_add_unknown_rfid")
    return raw is None or raw.lower() == "true"


async def _tagless_default(deps: PipelineDeps) -> dict | None:
    """The configured tagless default, through the ONE parser (``spool_tagless``)."""
    try:
        return await spool_tagless.tagless_default_filament(deps.db)
    except Exception:  # noqa: BLE001 — an unreadable setting is "feature off", never a crash
        logger.exception("[slot-state] tagless default lookup failed")
        return None


def _printer_busy(deps: PipelineDeps) -> bool:
    """RUNNING/PAUSE, read through ``ams_presence``'s one running-state predicate."""
    try:
        return ams_presence.printer_running(getattr(deps.client, "state", None))
    except Exception:  # noqa: BLE001 — an unreadable client is not evidence of a print
        return False


def _settling(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Either settle window (mint-settle F1 / config-settle) is open on this slot."""
    try:
        return spool_tagless.slot_is_settling(printer_id, ams_id, tray_id)
    except Exception:  # noqa: BLE001 — an unreadable window reads as settled
        return False


def _identify_in_flight(deps: PipelineDeps, ams_id: int) -> bool:
    """The client's read-only pre-flight: is a firmware RFID read running right now?

    Advisory by contract (invariant 2) — used ONLY to defer, never to pre-approve a
    write. ``"drying"`` is reported separately so the table can name the right reason.
    """
    client = deps.client
    if client is None:
        return False
    try:
        return client.ams_write_refusal(ams_id) in ("identifying", "identify_in_flight")
    except Exception:  # noqa: BLE001 — an unreadable client raises no objection
        return False


def _drying(deps: PipelineDeps, ams_id: int) -> bool:
    client = deps.client
    if client is None:
        return False
    try:
        return bool(client.ams_unit_drying(ams_id))
    except Exception:  # noqa: BLE001 — mirrors ams_presence.unit_drying's never-raise rule
        return False


# --- application ------------------------------------------------------------


async def _apply(
    obs: TrayObservation,
    deps: PipelineDeps,
    assignment: SpoolAssignment | None,
    decision: Decision,
    seen: set[int],
    tray_counts: dict[int, int],
) -> tuple[Decision, bool]:
    """Perform ``decision``. Returns the decision actually applied (a MINT may convert
    to a BIND) and whether the binding ledger changed."""
    kind = decision.kind
    # The row this slot held BEFORE the writer runs — captured as an id, not an object,
    # because the writer deletes the assignment and a detached instance is no longer a
    # readable source. It is what tells a bind whether it DISPLACED something
    # (:func:`_dispose_displaced`); a same-spool upsert displaces nobody.
    prior_spool_id = assignment.spool_id if assignment is not None else None

    if kind is DecisionKind.KEEP:
        # No cycle bookkeeping: a KEEP re-states the binding it found, so whatever
        # evidence is pending on the slot is still pending ABOUT that binding. Both
        # KEEP shapes want it left alone — the spent latch needs it for row 4a's
        # release, and a live tagless row's cycle belongs to the fresh-roll prompt lane,
        # which already made its own decision in the await that armed it.
        await _apply_keep(obs, deps, assignment, decision)
        return decision, False

    outcome: tuple[Decision, bool] | None = None
    if kind is DecisionKind.BIND:
        outcome = await _apply_bind(obs, deps, decision, seen, prior_spool_id)
    elif kind is DecisionKind.MINT:
        outcome = await _apply_mint(obs, deps, decision, seen, prior_spool_id)
    elif kind is DecisionKind.RECLAIM:
        outcome = await _apply_reclaim(obs, deps, decision, seen)
    elif kind is DecisionKind.RELEASE:
        outcome = await _apply_release(obs, deps, assignment, decision)
    elif kind is DecisionKind.REPLACE_SPENT:
        outcome = await _apply_replace_spent(obs, deps, assignment, decision, seen)
    if outcome is not None:
        _settle_physical_cycle(obs, kind, applied=outcome[1])
        return outcome

    # DEFER / NONE: the two identity-owed shapes buy an answer instead of guessing;
    # everything else is a deliberate no-op this push. The observation's tray dict rides
    # along as the evidence the need gate judges — the RAW push, same as everything else
    # this module decides on.
    if decision.reason in _IDENTIFY_VERDICT:
        printer_id, ams_id, tray_id = obs.slot
        # A STANDING constellation buys its OWN occasion, before the need gate is
        # consulted. Read occasions otherwise open only on a qualified physical cycle, a
        # terminal sweep, or a manual command — so when the insert's presence edges were
        # missed (2026-08-07, spool 226 on 001-H2S slot 1) such a verdict was a request
        # nothing could ever grant and the slot parked for a day. One read per EPISODE,
        # keyed inside ``ams_presence``, so a verdict re-derived at the push cadence
        # still costs exactly one read.
        if decision.reason == "spent_occupied_owed_identify" and prior_spool_id is not None:
            ams_presence.open_spent_occupied_occasion(printer_id, ams_id, tray_id, prior_spool_id)
        elif decision.reason == "identity_ambiguous_owed_full_read":
            # Episode = (the row we are disagreeing with, the tag doing the disagreeing).
            ams_presence.open_ambiguity_occasion(printer_id, ams_id, tray_id, prior_spool_id, obs.tag_uid)
        await deps.identify(printer_id, ams_id, tray_id, decision.reason, observation_tray_dict(obs))
    elif decision.reason == "unknown_tag_prompt_owed":
        # Auto-add is OFF and no row owns this roll: the table refuses to mint, so the
        # operator gets the durable-per-slot prompt instead.
        await _prompt_unknown_tag(obs, deps, tray_counts)
    logger.debug(
        "[slot-state] printer=%d A%dT%d %s reason=%s",
        obs.printer_id,
        obs.ams_id,
        obs.tray_id,
        kind.value,
        decision.reason or "-",
    )
    return decision, False


def _settle_physical_cycle(obs: TrayObservation, kind: DecisionKind, *, applied: bool) -> None:
    """Own the slot's pending physical cycle for the outcome THIS pass just landed.

    The cycle (``spool_tagless._pending_physical_cycles``) is per-slot swap currency: a
    measured ≥ ``_MIN_PHYSICAL_ABSENT_S`` absence→presence pair, armed at the gain edge and
    spent by exactly one consumer. Its lifecycle decision used to be taken in
    ``spool_tagless._maybe_prompt_fresh_roll`` — a PROMPT function, running inside the very
    await that arms the entry, i.e. BEFORE any outcome exists — which is why it could only
    guess: ``spool is None ⇒ discard``. That guess is precisely wrong for the shape it hits
    most, because an UNBOUND slot is exactly what a refill lands in during the ~3-minute
    bay-clear→HMS gap (shape 31): the roll's binding was released minutes before the
    firmware admitted the runout, the de-bounce re-binds the exhausted row, the runout then
    stamps ``spent_at`` on it — and row 4a needs a pending cycle to fire ``REPLACE_SPENT``.
    The prompt lane had already thrown it away, so the slot parked on ``KEEP("spent_latch")``
    until a human pulled and re-seated the roll (scenarios T8b/T8c — the cases the
    cause-based disqualification is structurally blind to, because the feeder had already
    moved or the loss-edge evidence died in a restart).

    So the decision moved HERE, where the outcome is known. One owner, one layer, and a
    reviewer can read the whole contract in one place:

    =================  =========  =====================================================
    Outcome            Cycle      Why
    =================  =========  =====================================================
    ``MINT``           discard    A fresh row has no spent transition coming. **This is
                                  the leak bound** — a cycle left pending forever would
                                  let a LATER spent binding on this slot fire
                                  ``REPLACE_SPENT`` with no physical event behind it.
    ``BIND``           discard    Same bound, other direction: another roll's row now
                                  holds the slot, so the departed roll's swap evidence
                                  is spent currency (this also preserves the prompt
                                  lane's old "not tagless ⇒ discard" outcome, which a
                                  tag-driven bind used to reach).
    ``RECLAIM``        preserve   The de-bounce re-bound an EXISTING row that evidence
                                  still in flight may yet declare exhausted. This is
                                  layer 2 of the gap fix — and the ONLY preservation with
                                  a deadline: it is MARKED
                                  (``spool_tagless.mark_cycle_preserved_by_debounce``) and
                                  retired at the printer's next JOB TERMINAL unless the
                                  bound row has been stamped spent by then
                                  (``expire_debounce_preserved_cycles``). The evidence it
                                  waits for has a ~3-minute life; without an owner the
                                  cycle sat forever and a genuine runout HOURS later could
                                  fire ``REPLACE_SPENT`` with no physical event behind it.
    ``REPLACE_SPENT``  consume    Unchanged: :func:`_apply_replace_spent` spends it
                                  itself, as the evidence it acted on.
    ``KEEP``           preserve   Never reaches here (see :func:`_apply`) — the spent
                                  latch's release signal and the fresh-roll prompt's own
                                  currency both survive a KEEP.
    =================  =========  =====================================================

    ``RELEASE`` is deliberately absent from the discarding set: the roll LEFT, and the
    departure's own artefacts (``last_location_*``, the dismissed prompt) are what the next
    arrival reads. Nothing new is asserted about the slot, so nothing is spent here.

    Gated on ``applied``: a decision the writer refused (the move damper, the per-pass
    seen-set, a vanished row) changed nothing, so the next push re-decides the same slot
    and must re-decide it on intact inputs.
    """
    printer_id, ams_id, tray_id = obs.slot
    if applied and kind is DecisionKind.RECLAIM:
        # Preservation with an owner: the de-bounce is the one outcome that KEEPS a cycle on
        # purpose, so it says so and the terminal hook can retire it (see the table above).
        spool_tagless.mark_cycle_preserved_by_debounce(printer_id, ams_id, tray_id)
        return
    if not applied or kind not in _CYCLE_DISCARDING_KINDS:
        return
    # Through the module's own pop — ONE implementation of "spend this slot's cycle",
    # shared by the consumer that acts on the evidence and the owner that retires it. The
    # log line is where the two intents differ, and the count of these is how a reviewer
    # confirms the bound is actually being applied.
    if spool_tagless.consume_qualified_cycle(printer_id, ams_id, tray_id):
        logger.debug(
            "[slot-state] printer=%d A%dT%d physical cycle discarded: %s left a different row on the slot",
            printer_id,
            ams_id,
            tray_id,
            kind.value,
        )


async def _apply_keep(
    obs: TrayObservation, deps: PipelineDeps, assignment: SpoolAssignment | None, decision: Decision
) -> None:
    """KEEP: the binding is correct. Two side-effects only, neither a binding change."""
    if decision.reason == "sibling_tag_read":
        await _record_sibling_tag(obs, deps, assignment, decision)
    if assignment is None or not obs.config_nonempty:
        return
    # Fingerprint refresh: the snapshot tracks what the slot currently reports, so a
    # re-configured (but same) roll does not read as a different filament on the next
    # push. Not a binding change — the same refresh both pre-cutover lanes performed on
    # a keep, now with one implementation.
    cur_color = obs.tray_color or ""
    cur_type = obs.tray_type or ""
    if (assignment.fingerprint_color or "").upper() == cur_color.upper() and (
        assignment.fingerprint_type or ""
    ).upper() == cur_type.upper():
        return
    assignment.fingerprint_color = cur_color
    assignment.fingerprint_type = cur_type
    await deps.db.commit()
    logger.debug(
        "[slot-state] printer=%d A%dT%d fingerprint refreshed to %s/%s (spool %s)",
        obs.printer_id,
        obs.ams_id,
        obs.tray_id,
        cur_type,
        cur_color,
        decision.spool_id,
    )


async def _record_sibling_tag(
    obs: TrayObservation, deps: PipelineDeps, assignment: SpoolAssignment | None, decision: Decision
) -> None:
    """Persist the roll's SECOND RFID chip onto its row, and announce it — ONCE, EVER.

    This is the one KEEP where the stored identity visibly disagrees with the wire (the
    uuid matched, the tag did not), so it is both the moment an operator needs the fact
    explained AND the moment we learn the other half of the roll's identity. Recording
    it is what makes the explanation a one-time event: with the pair on the row, every
    later read of that chip matches (:func:`tag_matches_row`) and resolves as a plain
    silent ``identity_matches_bound`` KEEP.

    The dedup this replaces was a process-lifetime set, so the six prod spools whose
    rolls sit facing their far side re-announced on every push after every restart,
    forever. A column cannot forget.

    THIRD distinct chip: refused, never overwritten. A genuine roll carries exactly two
    tags, so a third read over a uuid-matching binding is a misread or a chimera row —
    evidence to surface, never to absorb into the pair. The WARN is deduped only because
    the condition STANDS (it re-derives on every push until a human settles it) and a
    1 Hz warning buries the very fact it reports; the ledger holds an entry only for a
    physically-impossible read, i.e. nothing at all in a healthy fleet.

    Rides the pass's session — the same session and commit discipline as the fingerprint
    refresh below it.
    """
    spool = assignment.spool if assignment is not None else None
    scanned = (obs.tag_uid or "").strip().upper()
    if spool is None or not scanned:
        return

    if spool.sibling_tag_uid:
        key = (spool.id, scanned)
        if key not in _chimera_warned:
            _chimera_warned.add(key)
            logger.warning(
                "[sibling-tag] printer=%d A%dT%d spool=%s read a THIRD tag %s over the recorded pair "
                "(%s / %s) on a matching tray_uuid — a roll carries only two chips, so this is a "
                "misread or a chimera row; pair left untouched",
                obs.printer_id,
                obs.ams_id,
                obs.tray_id,
                spool.id,
                scanned,
                spool.tag_uid or "-",
                spool.sibling_tag_uid,
            )
        return

    spool.sibling_tag_uid = scanned
    await deps.db.commit()
    logger.info(
        "[sibling-tag] printer=%d A%dT%d spool=%s read its second tag %s (stored %s, tray_uuid %s) — "
        "pair recorded; further reads of either chip resolve silently",
        obs.printer_id,
        obs.ams_id,
        obs.tray_id,
        decision.spool_id,
        scanned,
        spool.tag_uid or "-",
        obs.tray_uuid or "-",
    )


async def _prompt_unknown_tag(obs: TrayObservation, deps: PipelineDeps, tray_counts: dict[int, int]) -> None:
    """Raise the operator's "add this roll" prompt for an unowned identity. Guarded."""
    printer_id, ams_id, tray_id = obs.slot
    try:
        await broadcast_unknown_tag(
            printer_id=printer_id,
            ams_id=ams_id,
            tray_id=tray_id,
            tag_uid=obs.tag_uid or "",
            tray_uuid=obs.tray_uuid or "",
            tray_type=obs.tray_type,
            tray_color=obs.tray_color,
            tray_sub_brands=obs.tray_sub_brands,
            tray_count=tray_counts.get(ams_id),
            emit=deps.emit,
        )
    except Exception:  # noqa: BLE001 — a prompt failure must not unwind the pass
        logger.exception("[slot-state] unknown-tag prompt failed for printer %d A%dT%d", printer_id, ams_id, tray_id)


def _duplicate_in_pass(obs: TrayObservation, spool_id: int | None, seen: set[int]) -> bool:
    """Second half of the flip-flop fix: one spool, one application, per pass.

    A roll cannot be in two trays, so a single push that decides to bind the SAME spool
    into two slots is wire churn (007-H2C spool 194: sticky identity across partial
    pushes presented one tag on two trays, and each pass produced two moves). The
    writer's move damper catches the cross-PASS half; this catches the within-pass half.
    """
    if spool_id is None or spool_id not in seen:
        return False
    logger.warning(
        "[slot-state] duplicate application skipped: printer=%d A%dT%d spool=%d already applied this pass",
        obs.printer_id,
        obs.ams_id,
        obs.tray_id,
        spool_id,
    )
    return True


async def _apply_bind(
    obs: TrayObservation, deps: PipelineDeps, decision: Decision, seen: set[int], prior_spool_id: int | None = None
) -> tuple[Decision, bool]:
    if _duplicate_in_pass(obs, decision.spool_id, seen):
        return decision, False
    spool = await deps.db.get(Spool, decision.spool_id)
    if spool is None:
        logger.warning(
            "[slot-state] printer=%d A%dT%d bind skipped: spool %s no longer exists",
            obs.printer_id,
            obs.ams_id,
            obs.tray_id,
            decision.spool_id,
        )
        return decision, False
    return await _bind_spool(obs, deps, decision, spool, seen, prior_spool_id=prior_spool_id)


async def _bind_spool(
    obs: TrayObservation,
    deps: PipelineDeps,
    decision: Decision,
    spool: Spool,
    seen: set[int],
    *,
    fingerprint: tuple[str, str] | None = None,
    prior_spool_id: int | None = None,
) -> tuple[Decision, bool]:
    """The shared BIND write: the ONE binding writer + the pre-config one-shot.

    Three flavours, all one write: a plain identity bind, the tagless pre-config
    one-shot (``pre_configured_apply`` — pushes the deferred config), and the two
    identity-CLAIM binds (:data:`_IDENTITY_CLAIM_REASONS` — link the tag onto the row,
    never push).
    """
    printer_id, ams_id, tray_id = obs.slot
    pre_config = decision.reason == "pre_configured_apply"
    claims_identity = decision.reason in _IDENTITY_CLAIM_REASONS
    pre_config_row = pre_config or decision.reason == "pre_configured_apply_identity"
    fp_color, fp_type = fingerprint if fingerprint is not None else (obs.tray_color or "", obs.tray_type or "")

    origin = ORIGIN_BIND
    if pre_config_row:
        origin = ORIGIN_PRECONFIG
    elif claims_identity:
        origin = ORIGIN_CLAIM

    assignment = await bind_spool_to_slot(
        deps.db,
        spool,
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        fingerprint_color=fp_color,
        fingerprint_type=fp_type,
        origin=origin,
    )
    if assignment is None:
        # Damped move — the writer already logged the WARNING and wrote nothing, so
        # there is no binding change to announce.
        return decision, False
    if claims_identity:
        # The roll is now positively identified and this row IS that roll, so the
        # observed identity lands ON it — one ledger row for one physical roll, with the
        # operator's own weight/label/cost intact. Through the existing linker so the
        # ``rfid_linked`` origin, the slicer-preset backfill and the INFO grammar stay
        # identical to the pre-cutover attract lane. It flushes; the commit is below.
        await link_tag_to_inventory_spool(deps.db, spool, observation_tray_dict(obs))
    if pre_config_row:
        # One-shot apply (the SpoolBuddy pre-config replay's semantics): the intent is now a
        # real location claim, so the marker is cleared. (The writer rebuilds the row, so
        # this is belt-and-braces on a column that already defaults to NULL — stated
        # explicitly because the marker's clearing is the semantic, not a side effect.)
        assignment.pre_configured_at = None
    await deps.db.commit()
    seen.add(spool.id)
    await _dispose_displaced(obs, deps, prior_spool_id, spool.id)
    if pre_config:
        # The deferred configuration finally goes out to the slot the firmware refused
        # it on while empty. NEVER on an identity-claim bind: that roll's configuration
        # is RFID-owned, and pushing ``ams_filament_setting`` over a BL-read slot
        # destroys the RFID-detected state (eye → pen in Studio) — the same no-push rule
        # ``spool_tag_matcher.auto_assign_spool`` states for BL spools.
        await deps.push_slot_config(spool, printer_id, ams_id, tray_id, observation_tray_dict(obs))
    await _emit_auto_assigned(obs, deps, spool)
    return decision, True


async def _apply_reclaim(
    obs: TrayObservation, deps: PipelineDeps, decision: Decision, seen: set[int]
) -> tuple[Decision, bool]:
    if _duplicate_in_pass(obs, decision.spool_id, seen):
        return decision, False
    printer_id, ams_id, tray_id = obs.slot
    spool = await deps.db.get(Spool, decision.spool_id)
    if spool is None:
        logger.warning(
            "[slot-state] printer=%d A%dT%d reclaim skipped: spool %s no longer exists",
            printer_id,
            ams_id,
            tray_id,
            decision.spool_id,
        )
        return decision, False
    assignment = await bind_spool_to_slot(
        deps.db,
        spool,
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        fingerprint_color=obs.tray_color or "",
        fingerprint_type=obs.tray_type or "",
        origin=ORIGIN_RECLAIM,
        # Doctrine rule 7: the SAME roll returning from a pull keeps its FIFO seating
        # position — the ordinal must not be re-stamped just because the row was rebuilt.
        preserve_ordinal=True,
        # …and, for the same reason, keeps its BIND MOMENT (:func:`_debounce_bind_moment`).
        bind_moment=_debounce_bind_moment(spool),
    )
    if assignment is None:
        return decision, False
    await deps.db.commit()
    seen.add(spool.id)
    absence = ams_presence.reseat_absence(printer_id, ams_id, tray_id)
    logger.info(
        "[slot-state] printer=%d A%dT%d de-bounced spool %d back onto its last location "
        "(measured absence %s, ordinal + bind moment preserved)",
        printer_id,
        ams_id,
        tray_id,
        spool.id,
        "unknown" if absence is None else f"{absence:.0f}s",
    )
    await _emit_auto_assigned(obs, deps, spool)
    return decision, True


def _debounce_bind_moment(spool: Spool) -> datetime | None:
    """When this roll's CURRENT seating began — the moment a de-bounce re-states.

    A de-bounce is the farm's assertion that **nothing physically happened**: the release
    it repairs was spurious, so the roll never left and its binding never really ended
    (doctrine rule 7 as amended 2026-08-19). Letting the writer stamp a fresh
    ``SpoolAssignment.created_at`` would say the opposite, because that column is the ONE
    "an unobserved swap happened at a KNOWN instant" boundary
    ``spool_tagless.reconcile_ledger_overcharges`` adjudicates across. Before the scoping
    that inference was merely weak; after it, the only reclaim that survives is one the
    farm has just certified as NOT a physical event — so a boundary stamped here would be
    actively false, and a de-bounced roll that later overshot its label would be split
    into a phantom successor for a swap that never occurred.

    ``loaded_at`` is that moment: the re-stampable FIFO ordinal, written by the binding
    writer at every genuine binding change and deliberately NOT re-written by this lane
    (``preserve_ordinal``), so it still names the last time a roll really did enter this
    slot. Using it keeps the reconciler's one remaining reachable trigger intact — an
    operator manually assigning an old row to a slot holding a new roll re-stamps
    ``loaded_at``, and a glitch after that carries THAT boundary forward rather than
    erasing it (scenario C5).

    The fallbacks are for rows predating the ordinal (``first_loaded_at``, then the row's
    own creation); all-NULL returns None and the writer's server default stamps now,
    which is the pre-wave behaviour and no worse than it was.
    """
    return spool.loaded_at or spool.first_loaded_at or spool.created_at


# --- release evidence (WS7 — diagnosis, never a decision) --------------------

# How fresh the last FULL status report must be for THIS push to be read as that same
# pushall. The AMS block rides the same ``print`` frame that carries ``sdcard``, and
# ``sdcard`` is what stamps ``PrinterState.last_full_report_at`` — so a pushall's own
# pipeline pass sees an age of ~0, while the ~1 Hz incrementals that follow it see a
# whole second or more. At 1 Hz the very first incremental AFTER a pushall is the one
# ambiguous case, which is why the raw age is logged BESIDE the label: the number is the
# fact, ``push=`` is only the reading of it.
_PUSHALL_SAME_FRAME_S = 1.0


def _presence_rule(obs: TrayObservation) -> str:
    """WHICH branch of ``tray_fields.tray_presence`` produced ``obs.present``.

    A mirror of that function's six-row branch table, kept here as a NAME generator
    only: it decides nothing, and the rule it reports is cross-checked against the
    presence the observation lane actually resolved. A mirror can drift from its
    original, so drift is made VISIBLE rather than silently reported as fact — a
    disagreement appends ``!drift`` instead of asserting a branch that did not run.
    That is the honest failure mode for a diagnostic whose whole purpose is to be
    believed six hours later by someone with no context.

    The two names that matter for a release are ``bit_clear`` (a TRUSTED in-push mask
    bit said the slot is bare — the invariant-12 authority wave 2 made
    release-authorizing, and therefore the branch a single glitched bit travels down)
    and ``cleared_shape`` (no in-push bit for this slot at all, so emptiness came from
    the fallback tier: the wire's own state-9 + asserted-empty ``tray_type``, or the
    shape ``bambu_mqtt._normalize_cleared_trays`` injects once the CACHED mask's veto
    lets it through). Telling those two apart at the release edge is most of the answer
    to "was this a real departure or a bit glitch?".
    """
    if obs.exist_bit is True:
        rule, implied = "bit_set", True
    elif obs.exist_bit is False:
        if obs.state in TRAY_PRESENT_STATES:
            rule, implied = "bit_clear_contradicted", None
        else:
            rule, implied = "bit_clear", False
    elif obs.state in TRAY_PRESENT_STATES:
        rule, implied = "state_present", True
    elif obs.state is not None and obs.tray_type == "":
        rule, implied = "cleared_shape", False
    else:
        rule, implied = "no_evidence", None
    return rule if implied is obs.present else f"{rule}!drift"


def _mask_facts(obs: TrayObservation, deps: PipelineDeps) -> tuple[str, str, str]:
    """``(mask, trusted, source)`` for the exist-bit evidence behind this release.

    The mask itself is only knowable from the client — ``PrinterState.ams_tray_exist_bits``
    / ``ams_bits_trusted`` are the triage surface wave 2 added for exactly this question —
    and that copy is the LAST bits-carrying push, which is not necessarily this one: the
    pipeline pass is scheduled off the raw AMS hook, so a later push can overtake it. The
    observation is what carries THIS push's answer for THIS slot (``obs.exist_bit``,
    already trust-filtered and unit-gated upstream), so the two are reported together and
    ``source`` says how they relate rather than pretending they are the same fact:

    * ``push`` — this push carried a trusted, unit-listed bit for the slot, and nothing in
      the cached hex contradicts it (it agrees, or the cache has no bit for this slot, or
      there is no cached hex at all), so ``mask`` may be read as the mask that decided;
    * ``push_cache_moved`` — this push carried the bit and the cached hex holds a DIFFERENT
      one for the same slot, which can only mean a later push overtook this pass. The
      deciding evidence is ``bit=``; the hex is a newer frame's, so never read it as the
      mask behind this release. The distinction is kept sharp deliberately: reporting an
      absent cache as "moved" would manufacture a wire anomaly out of a missing client;
    * ``cached`` — this push offered the slot NO bit (the H2S trap: some firmware paths
      send ``tray_exist_bits`` in pushalls only). Presence came from the tray-level
      fallback tier, where a cached mask can only ever act as the promote-only VETO in
      ``_normalize_cleared_trays`` — it may promote a stuck tray to seated, never demote
      one (``apply_tray_exist_bits allow_demote``), so a cached mask can permit this
      release but never author it;
    * ``none`` — no mask is known at all; the fallback tier ran unassisted.

    Trust is reported from the client's own verdict, except where this push's bit exists:
    an untrusted mask is stripped before the observation lane ever sees it, so a present
    ``obs.exist_bit`` is trusted by construction.

    Guarded on its OWN, not only by the caller: an unreadable client must cost the record
    its three mask fields (rendered ``?``, which is distinct from ``-``/``none`` meaning
    "genuinely absent") and nothing else. The observation half of the line is the half a
    triager needs most, and losing it because a disconnecting client raised would be the
    diagnostic failing exactly when something is going wrong.
    """
    try:
        state = getattr(deps.client, "state", None)
        cached_hex = getattr(state, "ams_tray_exist_bits", None)
        cached_bits = parse_tray_exist_bits(cached_hex)
        cached_bit = slot_exist_bit(cached_bits, obs.ams_id, obs.tray_id)
        mask = str(cached_hex) if cached_hex not in (None, "") else "-"

        if obs.exist_bit is not None:
            moved = cached_bit is not None and cached_bit is not obs.exist_bit
            return mask, "yes", ("push_cache_moved" if moved else "push")
        trusted = "yes" if getattr(state, "ams_bits_trusted", False) else "no"
        return mask, trusted, ("cached" if cached_bits is not None else "none")
    except Exception:  # noqa: BLE001 — an unreadable client is unknown evidence, never a crash
        return "?", "?", "?"


def _push_shape(deps: PipelineDeps) -> tuple[str, str]:
    """``(shape, age)`` — was this push a full report (pushall) or an incremental?

    Inferred from ``PrinterState.last_full_report_at`` against
    :data:`_PUSHALL_SAME_FRAME_S`; ``("?", "-")`` when the printer has never delivered a
    full report, and ``("?", "?")`` when the client could not be read at all, because
    "unknown" must never render as "incremental" and the two unknowns have different
    causes. Guarded here for the same reason :func:`_mask_facts` is.
    """
    try:
        last_full = getattr(getattr(deps.client, "state", None), "last_full_report_at", 0.0)
        try:
            last_full = float(last_full or 0.0)
        except (TypeError, ValueError):
            last_full = 0.0
        if last_full <= 0.0:
            return "?", "-"
        age = time.monotonic() - last_full
        return ("full" if age < _PUSHALL_SAME_FRAME_S else "incr"), f"{age:.1f}s"
    except Exception:  # noqa: BLE001 — an unreadable client is unknown evidence, never a crash
        return "?", "?"


def _was_feeding(printer_id: int, ams_id: int, tray_id: int) -> str:
    """``yes``/``no``/``?`` — was this slot the active feeder when it lost presence?

    Reads the LOSS-SIDE stamp (``ams_presence.absent_under_active_feed``), which is the only
    one that exists at a release: ``ams_presence`` keeps the same fact in two places, stamped
    at the loss edge (``_absent_under_active_feed``) and carried into ``_reseat`` at the gain,
    and ``_reseat`` is DROPPED at every loss. This field read the gain-side accessor
    (``reseat_under_active_feed``, which :func:`_runout_suspect` correctly uses to adjudicate
    a RETURN) and so could never answer ``yes`` — a diagnostic whose most load-bearing
    discriminator was structurally dead (2026-08-20).

    The ordering that makes this truthful is ``printer_manager._run_slot_pipeline_pass``':
    the presence pass derives THIS push's edges before the pipeline resolves the same push,
    so the loss edge is already stamped when the release is decided.

    ``?`` means the loss edge was never observed (a restart, a first-batch seed) or the
    ledger could not be read — never "not feeding". A PEEK, never consuming, and guarded so
    an unreadable ledger costs one field rather than the line.
    """
    try:
        fed = ams_presence.absent_under_active_feed(printer_id, ams_id, tray_id)
    except Exception:  # noqa: BLE001 — an unreadable ledger is unknown cause, never a crash
        return "?"
    if fed is None:
        return "?"
    return "yes" if fed else "no"


def _release_evidence(obs: TrayObservation, deps: PipelineDeps, spool: Spool | None, decision: Decision) -> str | None:
    """ONE greppable INFO line reconstructing why this release fired. Never raises.

    **Diagnosis, not a decision.** Nothing here is read by any branch; deleting this
    function would not move a single release by one push. It exists because the 8-day
    measurement that scoped the de-bounce lane (doctrine rule 7 as amended 2026-08-19)
    found **14 of 52 last-location reclaims returning in under five minutes — four at
    0.0 minutes, across four different printers, inside one minute, on the same spool**.
    No human pulls and re-seats a roll that fast, so those releases were SPURIOUS: the
    tray reported absent for a push while the roll sat physically seated, and the reclaim
    lane was silently repairing it. Since wave 2 made a cleared exist bit
    release-authorizing (invariant 12), one glitched bit hard-DELETES a binding — and a
    binding deletion is the event that starts the whole identity problem. The de-bounce
    contains the damage; it explains nothing. This line is what makes the NEXT occurrence
    explainable from the log alone, with no live reproduction.

    Every field is here because reconstructing the decision needs it, and nothing else is:

    * ``presence``/``rule`` — the tri-state the observation lane resolved and WHICH branch
      produced it (:func:`_presence_rule`). ``rule=bit_clear`` is the glitch-suspect path;
      ``rule=cleared_shape`` is the fallback tier and a different investigation entirely.
    * ``bit``/``state``/``type``/``cfg``/``id`` — the raw per-tray facts THIS push asserted,
      which are the rule's own inputs. ``cfg=0 id=0`` beside ``state=9`` is the minimal
      ``{id, state}`` partial; a full report asserts configuration, so these corroborate
      the push shape independently of the clock.
    * ``mask``/``mask_trusted``/``mask_src`` — the exist-bit mask, the trust verdict
      (the unit-present gate and the all-zero streak rule), and whether it is THIS push's
      or a cached copy (:func:`_mask_facts`). The promote-only cached asymmetry means a
      cached mask can permit a release but never author one.
    * ``push``/``push_age`` — pushall or incremental (:func:`_push_shape`). The known trap
      is that some H2S firmware paths carry ``tray_exist_bits`` in pushalls ONLY, so a
      release on an incremental had no in-push mask by construction.
    * ``feeding``/``printing`` — the CAUSE test, and the discriminator that separates a
      glitch from a real departure at the release edge. ``feeding=yes`` means this slot was
      the active feeder of a live print when it lost presence, i.e. a runout or a mid-print
      pull — never a glitch (operator ruling 15). Read at the release rather than queried
      from an incident on purpose: the AMS clears a drained slot's exist bit ~3 min BEFORE
      it declares the runout, so the incident does not exist yet, while the loss-edge feeder
      stamp does — ``ams_presence.on_tray_observations`` derives this push's edges before
      the pipeline resolves the same push. It is the LOSS-side stamp
      (``absent_under_active_feed``), not the gain-side ``reseat_under_active_feed`` the
      de-bounce adjudicates a return with: at a release the roll has not come back, so the
      gain-side ledger is empty by construction and ``feeding=`` could only ever read ``no``
      (:func:`_was_feeding`). ``?`` is an unobserved loss edge, never a negative.
    * ``spool``/``tagless``/``used_g``/``label_g`` — the subject and its grams: what a
      spurious release puts at risk, and whether the de-bounce could even take it back
      (only an untagged donor may).

    Correlating a T1 de-bounce against a genuine roll change takes this line plus the
    de-bounce's own (``_apply_reclaim``, which logs the MEASURED absence): same slot, same
    spool, a sub-window absence and ``feeding=no`` here is row T1; a long absence, or
    ``feeding=yes``, is a real departure.

    TWO release lanes deliberately do NOT emit this, and both exclusions are about having
    nothing true to say rather than about noise:

    * :func:`_release_orphan` — an orphan release is a DB-integrity event (the assignment
      outlived its spool row), with no grams to name and no wire evidence to weigh, so the
      writer's own line already says everything true about it;
    * the OPERATOR release, ``DELETE /api/v1/inventory/assignments/...``
      (``release_spool_from_slot(reason="operator_clear")``). That route has no
      ``TrayObservation`` at all — it is answering a human, not a push — so every field
      above except the spool's grams would be a fabrication, and the one question this
      record exists to answer ("was the departure real or a bit glitch?") is already
      answered by the fact that a person asked for it. ``spool_binding``'s own
      ``[slot-state] … release … reason=operator_clear`` line is the record for that lane.
      The route is untouched by design: a diagnostic must never grow a second, evidence-less
      shape to look complete.

    Returns the line, or ``None`` when the record could not be built. **A diagnostic that
    can raise is worse than no diagnostic at all** (cross-cutting invariant 10): the
    caller logs whatever comes back and releases either way.
    """
    try:
        printer_id, ams_id, tray_id = obs.slot
        mask, mask_trusted, mask_src = _mask_facts(obs, deps)
        push, push_age = _push_shape(deps)
        bit = "-" if obs.exist_bit is None else ("1" if obs.exist_bit else "0")
        presence = "unknown" if obs.present is None else str(obs.present)
        tray_type = "-" if obs.tray_type is None else (repr(obs.tray_type) if obs.tray_type == "" else obs.tray_type)
        used = "-" if spool is None or spool.weight_used is None else f"{float(spool.weight_used):.1f}"
        label = "-" if spool is None or spool.label_weight is None else f"{float(spool.label_weight):.1f}"
        tagless = "-" if spool is None else ("yes" if is_tagless_spool(spool) else "no")
        # A missing spool row is the orphan shape ``_release_orphan`` normally intercepts;
        # rendering it as "-" rather than crashing keeps the record honest if one ever
        # reaches here by another route.
        spool_id = "-" if spool is None else spool.id
        return (
            f"[slot-state] release-evidence printer={printer_id} A{ams_id}T{tray_id} "
            f"spool={spool_id} "
            f"reason={decision.reason or '-'} tagless={tagless} used_g={used} label_g={label} "
            f"presence={presence} rule={_presence_rule(obs)} "
            f"bit={bit} state={'-' if obs.state is None else obs.state} type={tray_type} "
            f"cfg={int(obs.config_asserted)} id={int(obs.identity_asserted)} "
            f"mask={mask} mask_trusted={mask_trusted} mask_src={mask_src} "
            f"push={push} push_age={push_age} "
            f"feeding={_was_feeding(printer_id, ams_id, tray_id)} "
            f"printing={'yes' if _printer_busy(deps) else 'no'}"
        )
    except Exception:  # noqa: BLE001 — a diagnostic may never cost a release (invariant 10)
        logger.exception("[slot-state] release-evidence record failed for slot %s", getattr(obs, "slot", "?"))
        return None


async def _apply_release(
    obs: TrayObservation, deps: PipelineDeps, assignment: SpoolAssignment | None, decision: Decision
) -> tuple[Decision, bool]:
    if assignment is None:
        return decision, False
    printer_id, ams_id, tray_id = obs.slot
    spool = assignment.spool
    # WS7: the record is BUILT here, while the binding and this push's observation still
    # sit side by side, and EMITTED after the write lands — so the line means "a release
    # happened", never "one was attempted". It reads nothing back through the session
    # afterwards, so it cannot be tripped by the commit.
    evidence = _release_evidence(obs, deps, spool, decision)
    prompt_cleared = await release_spool_from_slot(deps.db, assignment, reason=decision.reason)
    await deps.db.commit()
    if evidence is not None:
        logger.info("%s", evidence)
    if decision.reason == "cleared_tray":
        # The roll left, so a never-fed ``ams_auto`` row it displaced-and-abandoned has
        # nothing left to stand for: dispose it here rather than leave a 0 g ghost in
        # Inventory (prod 2026-08-07: spools 239-242). Ledger-bearing rows are ONLY
        # released — their grams are the tagless truth source (doctrine rule 4).
        await _dispose_ghost(obs, deps, spool, "released")
    if prompt_cleared:
        # The physical subject of the fresh-roll question left the slot, so every open
        # client must drop the toast it can no longer answer. Through the ONE dismissal
        # broadcaster (``spool_tagless``), same payload the operator answers produce.
        await _broadcast_prompt_dismissed(deps, printer_id, ams_id, tray_id)
    await _emit_assignment_changed(obs, deps)
    return decision, True


async def _broadcast_prompt_dismissed(deps: PipelineDeps, printer_id: int, ams_id: int, tray_id: int) -> None:
    """Drop a slot's stale fresh-roll toast fleet-wide. Guarded — a broadcast failure
    must never unwind a release that already landed (invariant 10)."""
    try:
        await spool_tagless.broadcast_tagless_fresh_dismissed(printer_id, ams_id, tray_id)
    except Exception:  # noqa: BLE001 — the stamp is already NULL; the toast is cosmetic
        logger.exception(
            "[slot-state] fresh-roll dismissal broadcast failed for printer %d A%dT%d", printer_id, ams_id, tray_id
        )


async def _dispose_ghost(obs: TrayObservation, deps: PipelineDeps, spool: Spool | None, event: str) -> bool:
    """Dispose a NEVER-FED auto-minted row that no longer holds its slot. True when disposed.

    "Ghost" is the fork's own provisional row (``data_origin`` ``ams_auto``) that the
    firmware never fed: minted from the tagless default while a slot was unresolved, then
    displaced by the real identity or abandoned when the tray emptied. It carries no
    grams, no operator edits and no identity, so leaving it makes Inventory a list of
    rolls that do not exist (prod 2026-08-07: spools 239-242, all 0 g, all unbound).

    ``spool_tagless.dispose_provisional_on_tag`` is THE disposal — it owns BOTH verdicts
    already: whose row this is (anything not auto-minted comes back "kept" and is left
    alone) and hard-delete vs archive (pristine vs ledger-bearing). So this adds exactly
    ONE gate on top of it and restates none of it: :data:`NEVER_FED_MAX_G` grams. A row
    that HAS fed is real consumption history whatever its origin (doctrine rule 4 — usage
    charges are the tagless truth source), and rule 8's warning that 0 g used ≠ never used
    is why the usage LEDGER still gets the final say inside the disposal.

    Rule 7 safety: a never-fed row's re-seat re-stamps ``loaded_at`` by definition
    (``spool_binding.stamp_loaded_for_slot``), so no FIFO seniority is destroyed here.
    """
    if spool is None or float(spool.weight_used or 0) >= NEVER_FED_MAX_G:
        return False
    printer_id, ams_id, tray_id = obs.slot
    spool_id = spool.id
    try:
        disposition = await spool_tagless.dispose_provisional_on_tag(deps.db, spool)
        if disposition == "kept":
            return False
        await deps.db.commit()
    except Exception:  # noqa: BLE001 — a failed disposal must not unwind the binding write
        logger.exception(
            "[slot-state] ghost disposal failed for spool %s (printer %d A%dT%d)", spool_id, printer_id, ams_id, tray_id
        )
        await _rollback(deps)
        return False
    logger.info(
        "[slot-state] printer=%d A%dT%d never-fed provisional spool %d %s (%s)",
        printer_id,
        ams_id,
        tray_id,
        spool_id,
        disposition,
        event,
    )
    return True


async def _dispose_displaced(
    obs: TrayObservation, deps: PipelineDeps, prior_spool_id: int | None, bound_spool_id: int
) -> None:
    """Route the row a successful bind just SUPERSEDED. Two lanes only.

    Called for the row that held this SLOT (both bind funnels) and for the row that owned
    this IDENTITY when a finished one was minted past (:func:`_apply_mint`, scenario G3) —
    one routing for both, because the justification below is written about the newcomer's
    evidence, not about which claim the old row was making.

    The writer's move semantics unbind the incumbent fleet-wide (one spool ⇔ at most one
    slot), which is correct for the ledger and silent about the row itself. Two kinds of
    superseded row must not survive as active inventory:

    * a never-fed ``ams_auto`` GHOST → :func:`_dispose_ghost` (the canonical disposal);
    * a SPENT row of any origin → archived. The newcomer's identity is positive proof the
      drained core physically left, which is the same evidence ``REPLACE_SPENT`` acts on
      — a spent core displaced by a real roll is exhausted trash, and an active spent row
      goes on presenting a 0 g ledger to the selection and deficit lanes.

    Every OTHER displaced row is left exactly as the writer left it: unbound inventory
    with its grams and its ``last_location_*`` reclaim stamp intact.
    """
    if prior_spool_id is None or prior_spool_id == bound_spool_id:
        return  # a same-spool upsert displaces nobody
    displaced = await deps.db.get(Spool, prior_spool_id)
    if displaced is None:
        return
    if await _dispose_ghost(obs, deps, displaced, f"displaced by spool {bound_spool_id}"):
        return
    if displaced.spent_at is None or displaced.archived_at is not None:
        return
    printer_id, ams_id, tray_id = obs.slot
    try:
        displaced.archived_at = datetime.utcnow()
        await deps.db.commit()
    except Exception:  # noqa: BLE001 — a failed archive must not unwind the binding write
        logger.exception("[slot-state] archiving displaced spent spool %d failed", displaced.id)
        await _rollback(deps)
        return
    logger.info(
        "[slot-state] printer=%d A%dT%d spent spool %d archived — displaced by spool %d",
        printer_id,
        ams_id,
        tray_id,
        displaced.id,
        bound_spool_id,
    )


async def _apply_mint(
    obs: TrayObservation, deps: PipelineDeps, decision: Decision, seen: set[int], prior_spool_id: int | None = None
) -> tuple[Decision, bool]:
    """MINT, with the last-second existence recheck.

    The table decides to mint from what the caller resolved a moment ago. Between then
    and now a row may have appeared (a concurrent pass, an operator add) — and some
    mint rows exist precisely for contract-violating shapes. Re-asking the DB is cheap;
    a twin ledger row for one physical roll is not (the sibling-tag failure mode).

    Exactly ONE row is never a valid conversion target, and the exception is the guard's
    own premise rather than a hole in it. A **FINISHED** roll (``Spool.is_finished_roll``
    — the ONE encoding, read here exactly as ``slot_state`` row 2.3a reads it) is not a
    live occupant: a runout means that row reached zero, filament cannot be added to a 0 g
    roll, so its tag reading back is a NEW roll on a reused core (doctrine rule 3 /
    operator ruling 3, scenario G3). Binding the newcomer onto it would BE the
    resurrection — the fresh roll inheriting a 0 g ledger and a spent latch, staging every
    run behind it — so the mint proceeds and the superseded owner is retired once the
    successor has actually landed. Everything else about the guard is untouched: a LIVE
    row owning this identity still takes the bind, one physical roll to one row.
    """
    superseded_owner_id: int | None = None
    if obs.identity_asserted:
        owner = await _identity_candidate(deps.db, obs)
        if owner is not None and owner.is_finished_roll:
            logger.info(
                "[slot-state] printer=%d A%dT%d mint kept: spool %d owns this identity but is a FINISHED roll "
                "— new roll on a reused core (doctrine rule 3)",
                obs.printer_id,
                obs.ams_id,
                obs.tray_id,
                owner.id,
            )
            superseded_owner_id = owner.id
        elif owner is not None:
            logger.info(
                "[slot-state] printer=%d A%dT%d mint converted to bind: spool %d already owns this identity",
                obs.printer_id,
                obs.ams_id,
                obs.tray_id,
                owner.id,
            )
            converted = Decision(DecisionKind.BIND, spool_id=owner.id, reason=decision.reason)
            if _duplicate_in_pass(obs, owner.id, seen):
                return converted, False
            return await _bind_spool(obs, deps, converted, owner, seen, prior_spool_id=prior_spool_id)

    spool, from_default = await _mint_from_spec(deps, obs, decision.mint_spec or {})
    fingerprint = _mint_fingerprint(obs, decision.mint_spec or {}, from_default)
    _decision, applied = await _bind_minted(
        obs, deps, decision, spool, seen, fingerprint, from_default, prior_spool_id=prior_spool_id
    )
    if applied and superseded_owner_id is not None:
        # The successor now carries this identity, so the finished row must stop owning
        # it. Not tidiness: :func:`_identity_candidate` answers "who owns this identity?"
        # with a ``LIMIT 1`` over ACTIVE rows, so two active owners make it answer
        # differently from one push to the next — and the roll's next slot move would find
        # the finished one, refuse it here again, and mint a THIRD row for the same
        # physical roll. That is the very failure this guard exists to prevent, so closing
        # the identity is part of minting past it, not a separate concern.
        #
        # Through the canonical displacement router: its spent lane archives (a soft hide
        # — grams and ``spent_at`` stay exactly as the runout left them, rule 8). Idempotent
        # against the call :func:`_bind_minted` already made for the slot's prior occupant,
        # which is the same row whenever the finished owner also held this slot.
        await _dispose_displaced(obs, deps, superseded_owner_id, spool.id)
    return _decision, applied


async def _bind_minted(
    obs: TrayObservation,
    deps: PipelineDeps,
    decision: Decision,
    spool: Spool,
    seen: set[int],
    fingerprint: tuple[str, str],
    from_default: bool,
    *,
    prior_spool_id: int | None = None,
) -> tuple[Decision, bool]:
    """Bind a freshly minted row. ``replace_existing`` is implicit: the writer's move
    semantics displace whatever held the slot, here and fleet-wide."""
    printer_id, ams_id, tray_id = obs.slot
    assignment = await bind_spool_to_slot(
        deps.db,
        spool,
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        fingerprint_color=fingerprint[0],
        fingerprint_type=fingerprint[1],
        origin=ORIGIN_MINT,
    )
    if assignment is None:
        # A fresh row holds no other binding, so it can never be a MOVE and the damper
        # cannot refuse it — this is pure defence in depth.
        await deps.db.commit()
        return decision, False
    await deps.db.commit()
    seen.add(spool.id)
    await _dispose_displaced(obs, deps, prior_spool_id, spool.id)
    if from_default:
        # The tray is bare or still carries the DEPARTED row's config, so the firmware
        # does not yet hold this row's identity — push it, exactly as the bare-tray
        # auto-config and ``_replace_row_after_cycle`` do.
        await deps.push_slot_config(spool, printer_id, ams_id, tray_id, observation_tray_dict(obs))
    await _emit_auto_assigned(obs, deps, spool)
    return decision, True


async def _page_reused_core_swap(obs: TrayObservation, deps: PipelineDeps, departed: Spool) -> None:
    """A reused-core swap (row 2.0) just concluded — WARN and page a human to verify it.

    **The conclusion is not in question; the PREMISE is.** Doctrine rule 3 says a spent RFID
    core can be refilled, so the table must be able to act on "a finished row's own tag reads
    back over a loaded tray" — that is a physical impossibility argument, not a heuristic, and
    hesitating would leave the fresh roll printing against a 0 g ledger. But doctrine rule 10
    is operator ground truth on THIS farm: **no RFID tag has ever been reused here** — and it
    was re-confirmed by the 2026-08-09 full-history audit, which found every respool prompt
    the farm has ever raised to be a false positive. Both are true at once, and they resolve
    into one instruction rather than a contradiction: the capability stays, and the FIRST
    time it fires it is evidence to VERIFY, not a routine event to swallow.

    So the swap always lands and it always pages. The two readings a human must choose
    between are named in the message:

    * a genuine first reuse — somebody refilled a Bambu core, which is supported and simply
      has never happened here yet;
    * a FALSE ``spent_at`` stamp — the roll never ran out, the ledger only thinks it did, and
      the stamp needs root-causing. Rule 8's asymmetry is why this is the live hypothesis: a
      missed stamp self-heals forward, a false one is permanent.

    A departing row that was **never fed** (``weight_used`` < :data:`NEVER_FED_MAX_G`) is the
    STRONGER form of the second reading and is called out by name: a roll that consumed
    nothing cannot have run out, so "spent" and "0 g delivered" cannot both be true.

    Reuses ``on_spent_contradiction`` rather than minting an event type: the fact reported is
    exactly that event's — a row the ledger calls SPENT is seated and reading loaded — and a
    second event for the same fact would split the operator's notification settings. No
    dedup is applied: unlike the standing-state detector that lane was built for, this fires
    on a TRANSITION that happens at most once per physical core, so there is nothing to
    re-notify about. There is deliberately no un-spend lane here either (operator ruling
    2026-08-09) — the page asks a human to look, it never edits the stamp.

    Fully guarded (invariant 10): a page is not allowed to unwind a swap that has already
    been decided, and the WARNING is emitted before anything that can fail.
    """
    printer_id, ams_id, tray_id = obs.slot
    delivered = float(departed.weight_used or 0)
    never_fed = delivered < NEVER_FED_MAX_G
    contradiction = (
        " and it was NEVER FED (a roll that consumed nothing cannot have run out, so the spent stamp is "
        "the thing to doubt)"
        if never_fed
        else ""
    )
    logger.warning(
        "[slot-state] printer=%d A%dT%d REUSED CORE: spent spool %d (tag %s, delivered %.1f g of %s g) read "
        "LOADED over this slot%s. Either this is the first RFID-tag reuse on this farm, or that spent stamp "
        "is false — verify. The swap was applied: the drained row keeps its spent_at and its grams, and the "
        "successor is bound carrying the tag.",
        printer_id,
        ams_id,
        tray_id,
        departed.id,
        departed.tag_uid or departed.tray_uuid or "-",
        delivered,
        "?" if departed.label_weight is None else f"{float(departed.label_weight):.0f}",
        contradiction,
    )
    try:
        from backend.app.models.printer import Printer
        from backend.app.services.notification_service import notification_service
        from backend.app.services.spool_recovery import runout_slot_desc

        printer = await deps.db.get(Printer, printer_id)
        label = f"{departed.material or 'filament'}, delivered {delivered:.0f} g"
        if never_fed:
            label += " — NEVER FED"
        await notification_service.on_spent_contradiction(
            printer_id,
            (printer.name if printer is not None else None) or f"Printer {printer_id}",
            departed.id,
            label,
            runout_slot_desc(encode_global_tray(ams_id, tray_id)) or f"AMS{ams_id}-T{tray_id}",
            # The firmware's own figure, or its own "unknown" sentinel — never a fabricated
            # fullness. Row 2.0's evidence is PRESENCE, not remain, so this is context for
            # the reader rather than part of the argument.
            obs.remain if isinstance(obs.remain, int) and 0 <= obs.remain <= 100 else -1,
            deps.db,
        )
    except Exception:  # noqa: BLE001 — a page may never unwind a decided swap (invariant 10)
        logger.exception(
            "[slot-state] reused-core swap notification failed for printer %d A%dT%d", printer_id, ams_id, tray_id
        )


async def _apply_replace_spent(
    obs: TrayObservation,
    deps: PipelineDeps,
    assignment: SpoolAssignment | None,
    decision: Decision,
    seen: set[int],
) -> tuple[Decision, bool]:
    """The W1 silent spent→mint: the drained row retires, its replacement takes the slot.

    This is the WIRE lane's spent→fresh transition. ``spool_tagless._replace_row_after_cycle``
    survives as the OPERATOR lane's executor (the "New roll" verb answering a fresh-roll
    prompt) and the two must stay behaviourally aligned; the ONE deliberate difference is
    stated here so it is not mistaken for drift: the departed row is disposed through the
    fork's canonical
    disposal (``dispose_provisional_on_tag``), so a PRISTINE auto-minted row (no usage
    ledger) is hard-deleted instead of leaving an archived 0 g husk, while any
    ledger-bearing row is archived exactly as before. A row that is not ours to dispose
    (operator-created) is archived too: the roll ran out and physically left, so it must
    not keep claiming a slot.

    Serves the table's spent-swap reasons, which differ only in what proves the roll
    changed (and therefore in the tray shape they accept — see the pre-gate):

    * ``spent_swap_confirmed`` — a QUALIFIED PHYSICAL CYCLE over a configured/fed tray. The
      cycle is consumed here so a later push cannot replay the swap.
    * ``spent_swap_no_tag_read`` — a commanded discovery read that ANSWERED NO TAG over a
      seated BARE tray whose binding is tagged (2026-08-07, spool 226). There is no cycle to
      consume; ``ams_presence``'s one-read-per-binding-epoch occasion is what makes that
      evidence non-repeating.
    * ``reused_core_swap`` — the bound row is a FINISHED roll and its OWN tag reads back
      over a seated tray (G3, doctrine rule 3). An identity read is the strongest evidence
      the farm has, and it is self-pacing: the swap it performs mints the successor onto
      the slot, so the next push resolves by identity (row 2.1 KEEP) and cannot replay it.
      It is also the ONE arm that pages on EVERY firing (:func:`_page_reused_core_swap`):
      doctrine rule 10 says no tag has ever been reused on this farm, so the first time
      this concludes, a human has to decide whether the premise finally changed or the
      ``spent_at`` stamp behind it is false.
    * ``operator_recheck_swap`` — the no-tag answer PLUS a human's answer (rule 12).

    Everything after the pre-gates is shared verbatim — one disposal, one mint funnel, one
    writer — and the INFO line names the reason so prod triage can tell them apart.

    The departed row is the slot's INCUMBENT (``assignment.spool``), and that is the arm's
    definition rather than an oversight: every ``REPLACE_SPENT`` the table emits passes
    ``spool_id=binding.spool_id``, and ``binding`` is :func:`_binding_view` of this very
    assignment — so ``decision.spool_id`` cannot name a different row, and re-reading it
    here would be a guard that can never fire. A conclusion about some OTHER row (row 2.3a's
    finished identity OWNER) deliberately does NOT route here for exactly that reason: this
    arm archives whatever holds the slot, so pointing it at a non-incumbent would retire the
    live roll standing in the tray. That case mints instead, and retires its superseded
    owner in :func:`_apply_mint`.
    """
    printer_id, ams_id, tray_id = obs.slot
    if assignment is None or assignment.spool is None:
        return decision, False
    cycleless_evidence = decision.reason in _CYCLELESS_SWAP_REASONS

    # Pre-gate: a dead roll re-seated without filament fed is not a swap — no churn
    # (mirrors ``spool_tagless``'s ``_tray_loaded`` gate in branch (3)). That flap
    # protection is CYCLE-shaped and stays EXACTLY as it was for ``spent_swap_confirmed``:
    # there, "this tray is configured or fed" is the only corroboration the physical cycle
    # has that a roll is really in the slot.
    #
    # The cycleless reasons corroborate differently and more strongly — the table already
    # required observation-asserted presence, plus an answered commanded read
    # (``spent_swap_no_tag_read``, whose tray is BARE by construction, so ``tray_loaded``
    # can only ever answer False for it) or an identity read (``reused_core_swap``, whose
    # own gate IS ``obs.present is True``). Re-assert the very presence the decision was
    # made on (the canonical tri-state rule, not a second state test that could disagree
    # with it); the read is what rules out a flap there.
    #
    # PRESENCE, not the seated state specifically. A no-tag answer read off a FEEDING
    # tray would prove nothing — the tag faces away from the reader once the filament is
    # threaded on to the hub — but such an answer cannot be obtained: a read is only ever
    # commanded when no filament is engaged (``command_identify``'s pre-check, mirroring
    # the client's own refusal), so every no-tag stamp this arm can see was taken on an
    # unengaged tray. Demanding state 10 HERE only recreated the emit-versus-grant
    # mismatch one layer down — the table decides on ``present is True``, so a slot that
    # was read bare and has since been fed produced a decision this gate silently threw
    # away, every push, forever.
    tray_ok = obs.present is True if cycleless_evidence else spool_tagless.tray_loaded(observation_tray_dict(obs))
    if not tray_ok:
        logger.debug(
            "[slot-state] printer=%d A%dT%d spent-replace skipped: tray present but not loaded (reason=%s)",
            printer_id,
            ams_id,
            tray_id,
            decision.reason,
        )
        return decision, False

    # Consume the qualified physical cycle — the ONE thing that releases the W1 latch,
    # and it is spent exactly here so a later push cannot replay the same swap. Only the
    # CYCLE-evidence reason has one to consume: the cycleless reasons rest on a READ
    # (whose one-per-epoch pacing lives in ``ams_presence``, or which is self-pacing for
    # ``reused_core_swap``), so demanding a cycle here would veto every swap they exist to
    # perform — and a G3 swap would then no-op on every push where no cycle happened to be
    # pending, leaving the fresh roll printing against the drained row's 0 g ledger until
    # the merged lane's respool tier 2 happened to converge on the same outcome.
    if decision.reason in _CYCLE_SPENDING_SWAP_REASONS:
        # These two SPEND a pending cycle but are never vetoed by its absence, and both
        # halves of that are load-bearing.
        #
        # Spending matters because a physical swap on this slot may well have been observed
        # — the operator's re-check was admissible precisely because an un-acted-on cycle
        # was standing, and a reused-core swap is a pull-and-reseat when the farm happens to
        # see the edges. Once acted on, that same cycle must not be replayable by a LATER
        # spent binding with no physical event behind it (the leak the outcome-driven
        # disposal below bounds).
        #
        # Requiring it would be wrong in the other direction: the evidence here is a READ.
        # A restart can erase the in-memory cycle while the re-check's DURABLE intent
        # survives (the whole reason the intent is durable), and a roll wound onto its own
        # core between two pushes may never present an absence long enough to qualify —
        # neither says anything about what the tag just proved.
        spool_tagless.consume_qualified_cycle(printer_id, ams_id, tray_id)
    elif not cycleless_evidence and not spool_tagless.consume_qualified_cycle(printer_id, ams_id, tray_id):
        logger.debug(
            "[slot-state] printer=%d A%dT%d spent-replace skipped: no qualified cycle to consume",
            printer_id,
            ams_id,
            tray_id,
        )
        return decision, False

    departed = assignment.spool
    # EVERY reused-core swap pages, before anything is disposed — the conclusion is
    # unconditional, the verification is not (see :func:`_page_reused_core_swap`).
    if decision.reason == _REUSED_CORE_SWAP_REASON:
        await _page_reused_core_swap(obs, deps, departed)
    disposition = await spool_tagless.dispose_provisional_on_tag(deps.db, departed)
    if disposition == "kept":
        departed.archived_at = datetime.utcnow()  # keep the ledger row + its grams
        disposition = "archived"
    await deps.db.flush()
    logger.info(
        "[slot-state] printer=%d A%dT%d spent spool %d %s on a roll swap (reason=%s)",
        printer_id,
        ams_id,
        tray_id,
        departed.id,
        disposition,
        decision.reason,
    )

    spool, from_default = await _mint_from_spec(deps, obs, decision.mint_spec or {})
    fingerprint = _mint_fingerprint(obs, decision.mint_spec or {}, from_default)
    # No ``prior_spool_id``: this arm disposed the departed row itself, above. Handing it
    # to the displacement router would ask a second lane to dispose an already-disposed
    # row — one disposal per departure, decided in one place.
    return await _bind_minted(obs, deps, decision, spool, seen, fingerprint, from_default)


# --- minting ----------------------------------------------------------------


async def _mint_from_spec(deps: PipelineDeps, obs: TrayObservation, spec: dict) -> tuple[Spool, bool]:
    """Create the row ``spec`` describes. Returns ``(spool, minted_from_default)``.

    Three shapes, one per existing minting owner — none of them re-implemented here:

    * ``source="tagless_default"`` → ``spool_tagless.mint_tagless_spool(default_filament=)``
      (data_origin ``ams_auto``, the default's slicer id + nozzle temps ride along so the
      slot stays a byte-identical firmware backup-group peer);
    * ``source="tray"`` WITH an identity → ``spool_tag_matcher.create_spool_from_tray``
      (the tagged auto-add lane: brand Bambu Lab, data_origin ``rfid_auto``);
    * ``source="tray"`` without one → ``mint_tagless_spool(tray=)``.
    """
    tray = _tray_dict_from_spec(obs, spec)
    if spec.get("source") == "tagless_default":
        default = spec.get("default_filament") or {}
        return await spool_tagless.mint_tagless_spool(deps.db, default_filament=dict(default)), True
    if spec.get("tag_uid") or spec.get("tray_uuid"):
        return await create_spool_from_tray(deps.db, tray), False
    return await spool_tagless.mint_tagless_spool(deps.db, tray=tray), False


def _mint_fingerprint(obs: TrayObservation, spec: dict, from_default: bool) -> tuple[str, str]:
    """``(fingerprint_color, fingerprint_type)`` for a minted row's binding.

    A default-minted row seeds its fingerprint from the SETTING, not the wire: the tray
    that triggers it reports an empty type, and an empty fingerprint is what the
    pre-configured marker used to be inferred from (``spool_tagless._assign_from_setting``).
    """
    if from_default:
        default = spec.get("default_filament") or {}
        return (default.get("rgba") or "", default.get("material") or "")
    return (obs.tray_color or "", obs.tray_type or "")


def _tray_dict_from_spec(obs: TrayObservation, spec: dict) -> dict:
    """Tray dict for a MINT: the spec is the authority on identity/config (it is what
    the table decided to mint), the observation supplies only slot address + state."""
    tray: dict = {"id": obs.tray_id}
    if obs.state is not None:
        tray["state"] = obs.state
    for key in (
        "tag_uid",
        "tray_uuid",
        "tray_type",
        "tray_color",
        "tray_info_idx",
        "tray_sub_brands",
        "remain",
        "nozzle_temp_min",
        "nozzle_temp_max",
    ):
        value = spec.get(key)
        if value is not None:
            tray[key] = value
    return tray


# --- websocket vocabulary ---------------------------------------------------


async def _emit_auto_assigned(obs: TrayObservation, deps: PipelineDeps, spool: Spool) -> None:
    """``spool_auto_assigned`` — the fork's existing assignment-announce vocabulary.

    ``origin="tagless"`` is added for a row with no RFID identity, which is what the
    frontend toasts on (``useWebSocket.ts`` case ``spool_auto_assigned``); a tagged
    payload stays byte-identical to the RFID lane's.
    """
    printer_id, ams_id, tray_id = obs.slot
    # The slot now has an owner, so any prompt raised for it is answered — drop the
    # dedup so a LATER unknown roll in this slot prompts again. This is the single
    # choke point every successful bind funnels through.
    clear_unknown_tag_dedup(printer_id, ams_id, tray_id)
    payload: dict = {
        "type": "spool_auto_assigned",
        "printer_id": printer_id,
        "ams_id": ams_id,
        "tray_id": tray_id,
        "spool_id": spool.id,
    }
    if is_tagless_spool(spool):
        payload["origin"] = "tagless"
    await deps.emit(payload)


async def broadcast_unknown_tag(
    *,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tag_uid: str,
    tray_uuid: str,
    tray_type: str | None = None,
    tray_color: str | None = None,
    tray_sub_brands: str | None = None,
    tray_count: int | None = None,
    emit: Broadcaster | None = None,
) -> None:
    """Raise the ``unknown_tag`` operator prompt for a slot, deduped per (slot, tag).

    Fired when auto-add is OFF and a roll no inventory row owns is sitting in a slot
    (decision ``unknown_tag_prompt_owed``), and by the Spoolman lane for the same
    situation on its side. Repeated MQTT pushes for one slot+tag must not spam the UI,
    so the tag tuple is remembered and only a CHANGE re-broadcasts.

    ``emit`` lets the pipeline route through :meth:`PipelineDeps.emit` (test-injectable);
    the Spoolman caller omits it and goes straight to the websocket manager.
    """
    slot_key = (ams_id, tray_id)
    tag_key = (tag_uid or "", tray_uuid or "")
    per_printer = _unknown_tag_last_broadcast.setdefault(printer_id, {})
    if per_printer.get(slot_key) == tag_key:
        logger.debug(
            "unknown_tag deduped for printer=%d AMS=%d slot=%d tag=%s",
            printer_id,
            ams_id,
            tray_id,
            tag_key[0][:8] or tag_key[1][:8] or "(none)",
        )
        return
    logger.info(
        "unknown_tag broadcast: printer=%d AMS=%d slot=%d type=%r color=%r tag=%s",
        printer_id,
        ams_id,
        tray_id,
        tray_type,
        tray_color,
        tag_key[0][:8] or tag_key[1][:8] or "(none)",
    )
    # Broadcast first; only commit the dedup if the WS write succeeds. If the broadcast
    # raises, the next push retries instead of being permanently silenced by a poisoned
    # dedup entry.
    payload = {
        "type": "unknown_tag",
        "printer_id": printer_id,
        "ams_id": ams_id,
        "tray_id": tray_id,
        "tag_uid": tag_uid,
        "tray_uuid": tray_uuid,
        "tray_type": tray_type,
        "tray_color": tray_color,
        "tray_sub_brands": tray_sub_brands,
        "tray_count": tray_count,
    }
    if emit is not None:
        await emit(payload)
    else:
        await ws_manager.broadcast(payload)
    per_printer[slot_key] = tag_key


def clear_unknown_tag_dedup(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Drop a slot's cached prompt tag, so the next unknown roll there re-prompts.

    Called when the slot reports EMPTY and when a bind resolves it — the two moments
    that make the previous prompt obsolete.
    """
    per_printer = _unknown_tag_last_broadcast.get(printer_id)
    if per_printer is None:
        return
    per_printer.pop((ams_id, tray_id), None)


async def _emit_assignment_changed(obs: TrayObservation, deps: PipelineDeps) -> None:
    """``spool_assignment_changed`` — the unbind half of the existing vocabulary
    (``api/routes/inventory.py``'s assign/unassign routes)."""
    printer_id, ams_id, tray_id = obs.slot
    await deps.emit(
        {
            "type": "spool_assignment_changed",
            "printer_id": printer_id,
            "ams_id": ams_id,
            "tray_id": tray_id,
        }
    )


__all__ = [
    "AppliedTransition",
    "PipelineDeps",
    "broadcast_unknown_tag",
    "clear_unknown_tag_dedup",
    "run_slot_pipeline",
]
