"""Tagless (non-RFID) spool support lanes — wire config, minting, operator verbs.

**Scope after the W3b cutover.** Deciding WHAT is in a tray and WHICH ledger row it
is belongs to ``slot_state`` (the decision table) and ``slot_pipeline`` (the one
orchestrator). This module no longer decides identity: its 7-branch
``handle_tagless_slot`` tree, the in-place ``_maybe_move_tagless_assignment`` rebind
and the terminal-time identity reconcile were deleted, not re-homed. What remains is
everything AROUND that decision, in three groups:

* **Minting + tagless defaults** — :func:`mint_tagless_spool`,
  :func:`tagless_default_filament`, :func:`canonical_default_identity`. The pipeline
  calls these to execute a MINT it decided on; the shapes are unchanged.
  :func:`canonical_default_identity` is the ONE predicate for "is this the fleet's
  default filament, spelled non-canonically?" — it replaced the three overlapping
  helpers (``override_generic_identity``, ``default_temps_for_fingerprint`` and the
  mint's inline override) in the 2026-08-21 backup-group wave, and it now covers the
  COLOUR dimension the earlier helpers left split.
* **Wire config (a sibling lane, NOT identity)** — :func:`maybe_autoconfigure_bare_tray`
  pushes the default filament to a BARE tray (spool present, ``tray_type`` empty,
  state 10/11) so the slot is usable, including mid-print where it joins the firmware
  backup pool (decision D3b). The pipeline classifies a bare tray
  ``OCCUPIED_UNRESOLVED`` and mutates no binding; this lane gives the firmware a
  configuration so the NEXT push carries a filament type and enters the pipeline's
  tagless lane. :func:`reconcile_slot_config` is its durable scheduler-tick retry lane
  (plus K-drift and owed-identify arms), because ``main.on_ams_change`` is
  change-gated and a settled AMS stops firing it (2026-07-24 incident). The retry is
  BOUNDED by a per-slot write epoch fed from the firmware's own ACKs
  (:func:`on_ams_command_result`): the wire refusing, or three attempts it never
  reflects, stops the lane for that slot until a presence/identity edge re-arms it.
* **Operator verbs + the prompts behind them** — the W5 fresh-roll prompt
  (:func:`note_physical_cycle` → :func:`_maybe_prompt_fresh_roll`, durable on
  ``Spool.fresh_prompt_pending_at``) and its executor :func:`apply_fresh_roll`, which
  answers "New roll" by archiving the current row and minting its replacement through
  :func:`_replace_row_after_cycle`. The WIRE equivalent of that transition is the
  pipeline's ``REPLACE_SPENT``; the two must stay behaviourally aligned.
  :func:`dispose_provisional_on_tag` retires an auto-minted provisional row when a
  real RFID tag claims its slot (hard-delete with no usage ledger, else archive).

The W1 spent latch itself is unchanged and now lives in the table (``spool.spent_at``
+ the binding IS the durable "this tray ran dry" state); this module still owns the
qualified-physical-cycle signal that releases it
(:func:`qualified_cycle_pending` / :func:`consume_qualified_cycle`).

Module edge state (``_autoconfig_window``, ``_autoconfig_epochs``,
``_pending_physical_cycles``, ``_settle_concluded_logged``) mirrors the fork's other
event-edge bookkeeping
(``spool_respool._last_tray_now``). It is lost on restart — worst case a bare-tray
config re-push waits one AMS push, a spent slot stays latched until a pull/reseat, a
write epoch starts over with full strikes (one more attempt, never a suppressed one;
still bounded by the ladder, and the wire re-states its verdict on that attempt), and
an unanswerable-identity conclusion logs its one INFO line a second time. The fresh-roll prompt is
deliberately NOT in that set: its state is the durable
``Spool.fresh_prompt_pending_at`` stamp, because a question nobody was connected to
hear must survive both an empty websocket list and a restart.

Binding writes (``_assign_from_setting``) and the FIFO stamps live in
``spool_binding`` — the lowest module every assignment-creating caller imports and
the single writer that enforces "one spool ⇔ at most one AMS slot, fleet-wide".
This module imports them so there is one implementation and no import cycle.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING, NamedTuple

from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from backend.app.core.websocket import ws_manager
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import ams_presence, spool_respool
from backend.app.services.printer_manager import printer_manager
from backend.app.services.spool_binding import bind_spool_to_slot, bound_elsewhere, last_released_from_slot_stmt
from backend.app.services.spool_tag_matcher import (
    auto_assign_spool,
    is_bambu_tag,
    is_valid_tag,
    parse_tray_fields,
    reapply_k_profile_if_drifted,
)
from backend.app.services.tray_fields import (
    backup_group_key,
    normalize_color_for_id,
    parse_int_field,
    tray_presence_from_dict,
)
from backend.app.utils.color_utils import colors_similar
from backend.app.utils.filament_ids import GENERIC_FILAMENT_IDS
from backend.app.utils.filament_types import canonical_filament_type
from backend.app.utils.retry_window import RetryWindow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Marker written on every auto-minted tagless row — the single classification the
# attract-exclusion, provisional-disposal, and terminal-sweep relax all key on.
DATA_ORIGIN = "ams_auto"

# Generic (GFx99) slicer ids — the fallback a bare-tray auto-config writes when no
# specific id is configured. A tray re-reporting one of these is untrustworthy for
# minting a fresh identity (W4 generic-id override).
_GENERIC_ID_VALUES = frozenset(GENERIC_FILAMENT_IDS.values())

# Re-push cadence for a BARE tray whose default-filament config has not yet
# landed on the printer (failed / slow MQTT). The trigger persists across AMS
# pushes until the firmware reports a non-empty tray_type; this gate stops it
# hammering the broker every push in the meantime.
_AUTOCONFIG_RETRY_S = 30.0

# Per-slot gate for that cadence. Cleared when the slot empties
# (:func:`clear_autoconfig_dedup`).
_autoconfig_window = RetryWindow(_AUTOCONFIG_RETRY_S)

# Config writes one slot may attempt before the lane concludes the write is not
# landing and stops. Each attempt is spaced by :data:`_AUTOCONFIG_RETRY_S`, so a
# strike always spans a full settle window — the firmware had 30 s to reflect the
# config and did not. Three is the smallest count that outlives the two ways a
# single attempt legitimately goes unreflected (a status report that crossed the
# write; one AMS busy window the publish gates did not catch) while bounding the
# blind-retry class at ~90 s. Unbounded is what this replaces: answered
# ``result:"fail"`` on every attempt, the lane re-published for hours (~40k writes
# a day, 2026-08-10) because nothing consumed the wire's answer.
_AUTOCONFIG_MAX_PUBLISHES = 3

# AMS ids the firmware uses for the EXTERNAL/virtual spool. A command echo for one
# of these is unaddressable here: :meth:`BambuMQTTClient.ams_set_filament_setting`
# rewrites the external coordinates on the way out (farm ``ams_id=255, tray_id=N``
# → wire ``ams_id`` 254/255 with ``tray_id`` 254, echoed back as slot position 0),
# and that mapping is not invertible from the echo alone. Ignored rather than
# guessed — the bare-tray lane only ever writes real AMS slots.
_ECHO_VIRTUAL_AMS_IDS = frozenset({254, 255})


class _AutoconfigEpoch(NamedTuple):
    """One run of config writes at a slot: first attempt → the wire's answer.

    ``publishes`` counts ATTEMPTS the lane committed to (the retry window is stamped
    and :func:`_push_config` called), which is what :data:`_AUTOCONFIG_MAX_PUBLISHES`
    bounds; ``acked_ok`` counts the ``result:"success"`` echoes among them, so the
    closing log can tell "the firmware said yes and did nothing" apart from "the
    firmware never answered". ``ended`` is the verdict once the lane has STOPPED
    writing (None while it is still trying) and its presence IS the latch: a slot
    with an ended epoch publishes nothing until an edge re-arms it.
    """

    publishes: int = 0
    acked_ok: int = 0
    ended: str | None = None


# (printer_id, ams_id, tray_id) -> that slot's open write epoch. Process memory by
# design: a restart drops every epoch, so the next pass starts a FRESH one and the
# ladder runs again. That is the safe direction (one more attempt, never a suppressed
# one) and it is bounded — a restart costs at most _AUTOCONFIG_MAX_PUBLISHES writes
# per bare slot, not the unbounded loop this replaces. Nothing durable is lost: the
# wire re-states its verdict on the very next attempt.
_autoconfig_epochs: dict[tuple[int, int, int], _AutoconfigEpoch] = {}

# Settle delay before a FRESH tagless row may be minted for a just-inserted spool.
# Inserting a roll makes the firmware publish the slot's config ~1 s BEFORE its own
# RFID read lands; minting on that first push auto-mints a tagless row that the tag
# read then destroys ("Provisional tagless spool N hard-deleted on RFID takeover" —
# three cases in one evening, 2026-07-19). Holding a fresh mint until the gain has
# settled lets the firmware's read win. Only FRESH mints wait: an existing binding
# (rebind / slot-move / spent-replace) is already the ledger's answer for the slot.
_MINT_SETTLE_S = 5.0

# (printer_id, ams_id, tray_id) of slots that saw a QUALIFIED physical roll swap
# (≥ _MIN_PHYSICAL_ABSENT_S absent → present, recorded by note_physical_cycle).
# This is the spent-binding latch's RELEASE signal, consumed by the pipeline's
# REPLACE_SPENT arm (via consume_qualified_cycle), by maybe_autoconfigure_bare_tray,
# and by the operator's New-roll verb, to mint the replacement over a spent row. A
# spent row with NO pending cycle stays latched (no phantom mint). Popped once
# processed on every path; process-lifetime (a swap during downtime degrades
# to a latched+excluded slot, released by pull/reseat — honest, not silent).
#
# SURVIVAL INVARIANT: a pending cycle on a SPENT-bound slot survives — regardless of
# the bound row's tag-ness — until consumed by exactly ONE of the pipeline's
# REPLACE_SPENT arm (:func:`consume_qualified_cycle`),
# :func:`maybe_autoconfigure_bare_tray`'s spent gate, or :func:`apply_fresh_roll`. Only
# NON-spent outcomes discard. :func:`_maybe_prompt_fresh_roll` upholds this by checking
# spent-ness FIRST: it runs inside the same await that ARMS the entry, so any earlier
# discard there destroys the release signal before a consumer can ever see it.
#
# …and it survives an UNBOUND slot for the same reason (2026-08-19, shape 32): the roll
# that ran out is released ~3 minutes BEFORE the firmware declares the runout, so a refill
# inside that gap resolves on an unbound slot whose spent stamp has not landed yet. This
# lane cannot know that outcome — it runs before the pipeline decides — so the discard
# decision for an unbound slot belongs to ``slot_pipeline._settle_physical_cycle``, which
# runs in the APPLY step of the deciding pass (MINT/BIND retire it; a de-bounce preserves
# it so the imminent spent stamp can still drive REPLACE_SPENT). Ownership, not politeness:
# a per-slot resource must not have its lifecycle decided by a function that cannot see
# what happened to the slot.
_pending_physical_cycles: set[tuple[int, int, int]] = set()

# The subset of :data:`_pending_physical_cycles` that is only still pending because a
# DE-BOUNCE preserved it, and which therefore needs an OWNER (2026-08-20).
#
# ``slot_pipeline._settle_physical_cycle`` preserves a cycle across a RECLAIM so the
# runout's imminent ``spent_at`` stamp can still drive ``REPLACE_SPENT`` on the next push
# (the T8b self-heal, layer 2 of the bay-clear→HMS gap fix). That preservation had no
# expiry at all: the evidence it stands on has a ~3-MINUTE life, but the cycle sat in the
# set indefinitely, so a spent stamp landing on that slot HOURS later — an ordinary,
# genuine runout of the very roll that de-bounced back — would fire ``REPLACE_SPENT`` with
# no physical event behind it, retiring the drained row and minting a successor onto a
# slot holding a state-11 drained core that nobody has touched.
#
# The bound is a CAUSE, not a timer (doctrine rule 6): the printer's next JOB TERMINAL.
# A job boundary is the natural scope for gap evidence — the runout that would justify the
# swap belongs to the print that was running when the bay emptied, and once that print has
# ended, a stamp arriving later belongs to a different story. See
# :func:`expire_debounce_preserved_cycles`.
_debounce_preserved_cycles: set[tuple[int, int, int]] = set()

# (printer_id, ams_id, tray_id) of slots whose config settle has already announced that
# the slot's identity is UNANSWERABLE this epoch (:func:`_config_settling`'s third arm).
# The gate is re-evaluated on every AMS push, so without this the one interesting line
# would repeat at ~1 Hz for as long as the tray stays engaged. Purely a log dedup: it
# gates NO decision, and the EPISODE it keys is the unanswered cycle itself — the marker
# is discarded the moment ``identity_unanswered`` goes False, so the NEXT unanswered
# cycle on the same slot announces its own conclusion.
_settle_concluded_logged: set[tuple[int, int, int]] = set()

# Fraction of a tagless row's label weight consumed past which a physical cycle
# raises the over-consumption / fresh-roll prompt (W5). 0.7 = the roll is ≥70 %
# consumed (≤300 g left on a 1000 g label) — operator setting 2026-07-20: a swap
# earlier in a roll's life is routine (drying, slot juggling) and asking then is
# noise.
_FRESH_ROLL_PROMPT_USED_FRAC = 0.7

# Minimum spacing between two :func:`reconcile_slot_config` passes. The scheduler
# tick that drives it is kick-driven (a dispatch burst wakes it repeatedly within
# seconds), so the pass carries its OWN floor instead of inheriting the tick
# cadence. Wide enough to cost nothing in steady state, tight enough that a
# refused AMS write is re-pushed within one settle window.
_RECONCILE_MIN_INTERVAL_S = 20.0

# gcode_state values during which the AMS is busy and silently ignores slot-config
# writes. Republishing into one of these does not converge — it just fills the log
# (2026-07-27: 998 lines over four hours on 002-H2S AMS0-T1). The reconcile lane
# keeps the OCCASION on a busy printer but skips the publish.
_CONFIG_PUSH_BUSY_STATES = ("RUNNING", "PAUSE")

# monotonic() stamp of the last reconcile pass — the only session state that lane
# keeps (everything else it acts on is derived from live state + DB each pass).
_last_reconcile_at: float | None = None


class _PresenceStaleEpisode(NamedTuple):
    """One continuous run of the SAME presence-stale reading on one bound slot.

    ``presence`` + ``spool_id`` are the episode's IDENTITY: while both hold, the farm is
    looking at one unchanging situation. ``first_seen`` is when that situation started,
    i.e. what :data:`_BOUND_PRESENCE_STALE_AFTER_S` is measured against; ``asks`` counts
    the requests made within it and ``next_ask_at`` is when the next one is due.
    """

    first_seen: float
    presence: bool | None
    spool_id: int
    asks: int
    next_ask_at: float


# (printer_id, ams_id, tray_id) -> the open episode for that slot, or absent when the
# slot is healthy/ineligible. Session state by design — a restart re-derives everything
# from the next pass's live state and the DB.
_presence_stale_episodes: dict[tuple[int, int, int], _PresenceStaleEpisode] = {}


class _SpentSwapParkEpisode(NamedTuple):
    """One continuous run of the SAME spent binding parked under a seated, configured tray.

    ``spool_id`` is the episode's identity: while that row still holds the slot and the
    tray still reads present+configured, the farm is looking at one unchanging park.
    ``first_seen`` is when it started; ``alerted`` records that the operator has been told
    about THIS episode, which is what makes the surface exactly one-per-episode.
    """

    first_seen: float
    spool_id: int
    alerted: bool


# (printer_id, ams_id, tray_id) -> the open spent-swap park for that slot. Deliberately a
# SEPARATE map from :data:`_presence_stale_episodes`: the two arms watch opposite
# predicates (spent vs non-spent, present vs absent-or-unknown, configured vs any) and
# folding them into one keyed state would make "which situation is this slot in?"
# unanswerable — the same conflation the 2026-08-19 wave deleted from ``_is_tagless``.
_spent_swap_park_episodes: dict[tuple[int, int, int], _SpentSwapParkEpisode] = {}

# How long ONE presence-stale reading must stand before the farm stops waiting for the
# wire to volunteer an answer. Deliberately well past every ORDINARY stale window: mid-
# print the H2S reduces its tray blocks to presence-unknown partials for the whole job,
# and eject/settle gaps are minutes at most. Past 15 minutes the reports have simply
# stopped carrying what the binding needs, and the two parties who can supply it — the
# printer (a fresh full report) and the operator (a standing-unknown toast) — are asked.
_BOUND_PRESENCE_STALE_AFTER_S: float = 900.0

# Gaps between the successive ASKS inside one unresolved episode, measured from the ask
# before. One ask per episode was the previous rule and its reasoning does not survive
# contact with the failure it was written for: "a repeat ask answers nothing" holds only
# if the first ask ARRIVED, and the ways it silently does not — a disconnected printer, the
# 120 s pacing floor already spent by the wire-side drain, a report that crossed the
# request — are exactly the states a slot sits in when its presence has stopped resolving.
# Silence after one unanswered request is indistinguishable from a printer that answered,
# so the episode keeps asking: +10 min covers the transient blockers, +1 h covers a
# printer that was busy for a whole job. Backoff, not repetition — a pushall is one ~8.7 KB
# report and nothing else, so the cost of asking again is wire chatter alone.
_PRESENCE_ASK_GAPS_S: tuple[float, ...] = (600.0, 3600.0)

# Steady-state spacing once the ladder above is exhausted. An episode that has survived
# three asks is a genuinely unanswerable slot (the A1/P1S always-``state=3`` dialect is the
# permanent case), and one quiet request an hour keeps the question live for the day the
# printer starts answering again without becoming the log's own noise.
_PRESENCE_ASK_INTERVAL_S: float = 3600.0

# The ask at which the farm stops asking the PRINTER quietly and tells the OPERATOR. The
# machine has now had every chance the ladder above allows; past this the unresolved slot
# is a human's problem, and it is raised exactly once per episode. This rung IS the toast's
# pacing (since 2026-08-11 this lane is the event's only emitter and it carries no dedup of
# its own): reaching it costs a full ladder, so two successive episodes on one slot are
# ≥85 min apart.
_PRESENCE_ASK_ESCALATE_AT: int = len(_PRESENCE_ASK_GAPS_S) + 1

# How long a SPENT binding may sit parked under a seated, configured tray before the
# operator is told. Its own constant, not a reuse of
# :data:`_BOUND_PRESENCE_STALE_AFTER_S`, because it is measured against a different
# situation: there the wire has stopped answering, here the wire is answering perfectly
# and the farm simply has no evidence it is allowed to act on. The value matches because
# the judgement is the same — a quarter of an hour is past every ordinary transient (a
# runout's own recovery, a print finishing, an eject) — and pinning them to one another
# would make a later change to either silently move the other.
_SPENT_SWAP_PARK_AFTER_S: float = 900.0

# Settle delay before the farm may publish a filament IDENTITY into a slot. A spool
# inserted into a slot that still carries a surviving tagless binding looks BARE for
# ~1 s while the firmware runs its own RFID read; an ``ams_filament_setting`` write
# landing in that window destroys the RFID-detected state and the firmware never
# retries (2026-07-25: a fresh Bambu-tagged roll sat as the tagless default with a
# phantom binding for 6 h). 30 s ≈ the fork's own identify bounds
# (``ams_presence._IDENTIFY_ACTIVE_S`` / the client's ``_IDENTIFY_GATE_S``), i.e. the
# window in which a firmware read may still be running.
_CONFIG_SETTLE_S = 30.0

# Hard cap on the second settle arm (an UNANSWERED physical cycle — the farm does not
# yet know what is in the slot). After 10 minutes no firmware read can still be
# running, so the gate fails OPEN: a genuinely tagless roll is delayed once, never
# stranded, and the reconcile lane guarantees the eventual push either way.
_CONFIG_SETTLE_MAX_S = 600.0


def _reset_state() -> None:
    """Test hook: clear module-level edge/dedup state between cases."""
    global _last_reconcile_at
    _autoconfig_window.reset()
    _autoconfig_epochs.clear()
    _pending_physical_cycles.clear()
    _debounce_preserved_cycles.clear()
    _settle_concluded_logged.clear()
    _presence_stale_episodes.clear()
    _spent_swap_park_episodes.clear()
    _last_reconcile_at = None


# --- state / predicate helpers ---------------------------------------------


def _norm_state(raw: object) -> int | None:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def tray_present(tray: dict) -> bool:
    """Positive-evidence presence: seated/loaded (state 10 or 11) only.

    Matches ``ams_presence._tray_present`` — state 9/None/unknown read as absent
    so an H2C idle-empty (state 0) never reads as a phantom spool.
    """
    return _norm_state(tray.get("state")) in (10, 11)


def _tray_loaded(tray: dict) -> bool:
    """Filament-loaded heuristic — mirrors ``spool_respool._tray_loaded``.

    state == 11 (fed to extruder) OR a non-empty tray_type when state is NOT one
    of the firmware's explicit empty signals (9, 10). A spent spool re-inserted
    without a refill reads present-but-not-loaded → False → no fresh-mint churn.
    """
    cur_state = tray.get("state")
    cur_type = (tray.get("tray_type") or "").strip()
    return cur_state == 11 or (cur_state not in (9, 10) and bool(cur_type))


def tray_loaded(tray: dict) -> bool:
    """Public read-only view of :func:`_tray_loaded` for the slot pipeline (W3a).

    The spent→mint transition must not fire on a dead roll that was re-seated without
    filament being fed, and that gate is this predicate — so the orchestrator asks the
    owner rather than re-deriving it (one implementation, no drift).
    """
    return _tray_loaded(tray)


def _tray_material(tray: dict) -> str:
    """Best-effort material string from a tray dict (no DB), for fingerprinting."""
    tray_type = (tray.get("tray_type") or "").strip()
    if tray_type:
        return tray_type
    sub = (tray.get("tray_sub_brands") or "").strip()
    return sub.split(" ", 1)[0] if sub else ""


def is_tagless_spool(spool: Spool | None) -> bool:
    """True when the spool carries NO RFID identity at all.

    All three identity columns count, ``sibling_tag_uid`` included: a Bambu roll carries
    two chips sharing one ``tray_uuid`` and either one identifies it (invariant 1 — the
    one either-chip law), so a row that has ever had its far side read is tag-identified
    even in the (physically odd) state where the near ``tag_uid`` is blank. Reading only
    ``tag_uid``/``tray_uuid`` here would call such a row tagless and hand it to the lanes
    that are allowed to REPLACE a tagless row's identity on assumption-tier evidence.
    """
    if spool is None:
        return False
    return not (spool.tag_uid or spool.tray_uuid or spool.sibling_tag_uid)


def fingerprint_matches(spool: Spool, tray: dict) -> bool:
    """Same physical filament: color within tolerance AND same canonical material."""
    if not colors_similar(tray.get("tray_color") or "", spool.rgba or "FFFFFFFF"):
        return False
    return canonical_filament_type(_tray_material(tray)) == canonical_filament_type(spool.material or "")


def _fingerprint_matches_default(material: str | None, rgba: str | None, default: dict) -> bool:
    """True when a (material, rgba) pair fingerprint-matches the tagless default —
    same canonical material AND color within tolerance. The material+rgba twin of
    :func:`fingerprint_matches` (a dict default has no tray shape), and the inner
    match of :func:`canonical_default_identity`'s eligibility test."""
    if canonical_filament_type(material or "") != canonical_filament_type(default.get("material") or ""):
        return False
    return colors_similar(rgba or "", default.get("rgba") or "")


def _base_filament_id(slicer_filament: str | None) -> str:
    """The bare preset id from a stored or wire reference (``"GFG99_x"`` → ``"GFG99"``).

    Mirrors the split ``slicer_filament_resolver`` does before it normalises, so the
    canonicaliser reads a suffixed id the same way the resolver does — otherwise a
    variant-suffixed generic id would look like an operator's specific preset and be
    left to split the backup group.
    """
    sf = (slicer_filament or "").strip()
    return sf.split("_", 1)[0] if "_" in sf else sf


def _eligible_for_default_identity(
    default: dict, *, slicer_filament: str | None, material: str | None, rgba: str | None
) -> bool:
    """Is this identity the tagless default's filament, merely spelled differently?

    Three conditions, all necessary:

    * the canonical MATERIAL matches the default's, and
    * the COLOUR is within ``colors_similar``'s tolerance of the default's
      (:func:`_fingerprint_matches_default`) — ``161616FF`` beside ``000000FF`` is one
      black PETG with two spellings, ``FF0000FF`` is a different roll; and
    * the stored PRESET id is not a different SPECIFIC one. Unstated (``""``/None) and
      GENERIC (``GFG99`` …) ids both assert nothing about which preset the roll is, so
      both are canonicalisable, and the default's own id is trivially so. A different
      specific id — ``GFG00`` beside a ``GFG02`` default — is an OPERATOR STATEMENT and
      is never rewritten on ANY dimension (doctrine rule 2).

    Split out of :func:`canonical_default_identity` because the harmonise arm has to
    ask "is this slot ours to canonicalise?" separately from "does anything differ?" —
    one origin for the eligibility test, two questions asked of it.
    """
    if not _fingerprint_matches_default(material, rgba, default):
        return False
    stored = _base_filament_id(slicer_filament)
    if not stored or stored in _GENERIC_ID_VALUES:
        return True
    return stored == (default.get("slicer_filament") or "").strip()


def canonical_default_identity(
    default: dict | None,
    *,
    slicer_filament: str | None,
    material: str | None,
    rgba: str | None,
    nozzle_temp_min: int | None,
    nozzle_temp_max: int | None,
) -> dict | None:
    """The tagless default's identity to write instead of the one given — or ``None``.

    THE predicate for "is this the fleet's default filament, spelled non-canonically?"
    and the single replacement for the three overlapping helpers this module used to
    carry (``override_generic_identity``, ``default_temps_for_fingerprint`` and the
    mint's own inline override): one question, four fields, one answer.

    Returns the default's ``{slicer_filament, rgba, nozzle_temp_min, nozzle_temp_max}``
    when the identity is ELIGIBLE (:func:`_eligible_for_default_identity`) **and** any
    of those fields differs from the default's — the preset id, the EXACT colour
    (through ``tray_fields.normalize_color_for_id``, so ``161616FF`` differs from
    ``000000FF`` even though the two are ``colors_similar``), or either nozzle temp.
    ``None`` means "nothing to do": already canonical, or not the default's filament at
    all. A field the default does not carry comes back ``None`` — every caller applies
    only what it is given, and never invents a field.

    Two of those four are the firmware's backup-group dimensions and two are not: the
    AMS pairs slots on PRESET + COLOUR only (``tray_fields.backup_group_key``, measured
    2026-08-25 off the firmware's own ``filam_bak`` groups). The nozzle temps are
    written in the same breath because they are part of the fleet default's identity and
    a slot carrying a stale range is a divergence an operator reads on the screen — NOT
    because they group, and re-deriving a grouping claim from their presence here is the
    mistake this paragraph exists to stop.

    Why the colour comparison is EXACT while eligibility is fuzzy:
    the firmware pairs slots into an auto-refill backup group only on an exact
    PRESET + COLOUR match. 011-H2S (2026-07-19) proved the preset dimension — one slot
    left on generic ``GFG99`` beside a ``GFG02`` peer never got a backup switch.
    010-H2S (2026-08-21) proved the colour dimension: slots 1+2 carried
    ``161616FF`` (a Bambu Studio / touchscreen slot edit that accepted the PETG-HF
    preset's own default colour) while slots 3+4 carried the farm's ``000000FF``, and
    the printer ran dry on slot 2 twice in 28 h with a full black roll one slot away —
    ``hms-events`` shows 69 auto-switches inside the 1↔2 pair, 14 inside 3→4, and none
    across. ``colors_similar`` answers "is this the same FILAMENT?" (eligibility);
    byte-exact equality answers "will the FIRMWARE pair them?" (is a write owed).
    Conflating the two is precisely how the farm harmonised the preset for three weeks
    (and the temperature range, which turned out to group nothing) while leaving the
    colour dimension split.

    Pure and synchronous: the caller supplies the parsed ``tagless_default_filament``
    dict (:func:`tagless_default_filament`), so this stays a decision with no I/O and
    the settings read happens once per lane instead of once per dimension. A ``None``
    default (feature off) answers ``None``.
    """
    if not default:
        return None
    if not _eligible_for_default_identity(default, slicer_filament=slicer_filament, material=material, rgba=rgba):
        return None

    canon_id = (default.get("slicer_filament") or "").strip() or None
    canon_rgba = (default.get("rgba") or "").strip() or None
    canon_min = parse_int_field(default.get("nozzle_temp_min"))
    canon_max = parse_int_field(default.get("nozzle_temp_max"))

    differs = (
        (canon_id is not None and _base_filament_id(slicer_filament) != canon_id)
        or (canon_rgba is not None and normalize_color_for_id(rgba) != normalize_color_for_id(canon_rgba))
        or (canon_min is not None and parse_int_field(nozzle_temp_min) != canon_min)
        or (canon_max is not None and parse_int_field(nozzle_temp_max) != canon_max)
    )
    if not differs:
        return None
    return {
        "slicer_filament": canon_id,
        "rgba": canon_rgba,
        "nozzle_temp_min": canon_min,
        "nozzle_temp_max": canon_max,
    }


def _tray_canonical_delta(default: dict | None, tray: dict) -> dict | None:
    """:func:`canonical_default_identity` for a LIVE tray dict.

    The ONE mapping from wire fields to the predicate's arguments, so the reconcile
    walk's ADOPTION test and the harmonise arm's WIRE step can never read the same tray
    differently — the two answers have to agree or the arm writes to a slot the walk has
    just declared settled, forever.
    """
    return canonical_default_identity(
        default,
        slicer_filament=(tray.get("tray_info_idx") or "").strip(),
        material=_tray_material(tray),
        rgba=(tray.get("tray_color") or "").strip(),
        nozzle_temp_min=parse_int_field(tray.get("nozzle_temp_min")),
        nozzle_temp_max=parse_int_field(tray.get("nozzle_temp_max")),
    )


# --- setting helpers --------------------------------------------------------


async def _tagless_default(db: AsyncSession) -> dict | None:
    """Parse the ``tagless_default_filament`` setting; None when empty (feature off).

    Unset (no DB row) resolves to the schema default (Bambu Lab PETG HF) — the
    feature is on by default. A stored empty string is the operator's explicit
    "off" and returns None. A shape without material/rgba is treated as off.
    """
    from backend.app.api.routes.settings import get_setting
    from backend.app.schemas.settings import _DEFAULT_TAGLESS_FILAMENT_JSON

    raw = await get_setting(db, "tagless_default_filament")
    if raw is None:
        raw = _DEFAULT_TAGLESS_FILAMENT_JSON
    raw = raw.strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("tagless_default_filament is not valid JSON — treating as feature off")
        return None
    if not isinstance(parsed, dict) or not parsed.get("material") or not parsed.get("rgba"):
        return None
    return parsed


async def tagless_default_brand(db: AsyncSession) -> str:
    """The configured tagless-default filament brand, or "" when unset/off.

    Public accessor over :func:`_tagless_default` (the single JSON parser) so
    other services — e.g. the re-spool tier-2 auto-brand fallback — share ONE
    source of truth for the ``tagless_default_filament`` setting without copying
    the parse or reaching into a module-private helper. Returns "" when the
    feature is off (parser returns None) or the configured JSON carries no brand,
    so callers never invent a brand.
    """
    default = await _tagless_default(db)
    return ((default or {}).get("brand") or "").strip()


async def tagless_default_filament(db: AsyncSession) -> dict | None:
    """The configured tagless-default filament dict, or None when the feature is off.

    Public accessor over :func:`_tagless_default` (the single JSON parser) for the slot
    pipeline, which hands the dict to the decision table as
    ``ResolutionContext.tagless_default``. Same contract as the private parser — no
    copy of the parse, no second source of truth for the setting.
    """
    return await _tagless_default(db)


# --- minting ----------------------------------------------------------------


async def mint_tagless_spool(
    db: AsyncSession, *, tray: dict | None = None, default_filament: dict | None = None
) -> Spool:
    """Mint a silently-tracked tagless spool from ONE of two sources.

    * ``tray`` — a configured AMS tray dict (identity via :func:`parse_tray_fields`).
    * ``default_filament`` — the ``tagless_default_filament`` setting dict
      (brand/material/subtype/rgba/slicer_filament), used for bare trays.

    Exactly one source must be given. Common shape: no tag fields,
    ``data_origin="ams_auto"``, ``tag_type=None``, ``weight_used=0``.
    ``label_weight`` uses the tray's reported net weight when the AMS gives a
    positive one, otherwise it is left to the ``Spool`` model's default (see
    ``models/spool.py`` — ``label_weight`` default = 1000 g); tagless trays
    commonly report ``tray_weight="0"``.
    """
    if (tray is None) == (default_filament is None):
        raise ValueError("mint_tagless_spool requires exactly one of tray / default_filament")

    label_weight: int | None
    if tray is not None:
        parsed = await parse_tray_fields(db, tray)
        material = parsed.material
        subtype = parsed.subtype
        color_name = parsed.color_name
        rgba = parsed.rgba
        brand = None  # tagless: brand unknown (third-party) — the operator can set it
        core_weight = parsed.core_weight
        slicer_filament = parsed.slicer_filament
        slicer_filament_name = parsed.slicer_filament_name
        nozzle_temp_min = parsed.nozzle_temp_min
        nozzle_temp_max = parsed.nozzle_temp_max
        # CANONICAL-IDENTITY guard: a tray re-reporting the GENERIC id an earlier
        # bare-tray auto-config wrote, or the near-black colour a touchscreen slot edit
        # left behind, mints the tagless default's OWN identity instead — id, colour and
        # temps together (:func:`canonical_default_identity`). All four fields, because
        # a row born canonical on the others is still not a firmware backup-group peer
        # while its COLOUR is off: 010-H2S ran dry twice in 28 h on rows that matched the
        # default's preset and carried 161616FF for its colour.
        _canon = canonical_default_identity(
            await _tagless_default(db),
            slicer_filament=slicer_filament,
            material=material,
            rgba=rgba,
            nozzle_temp_min=nozzle_temp_min,
            nozzle_temp_max=nozzle_temp_max,
        )
        if _canon is not None:
            if _canon["slicer_filament"] and _canon["slicer_filament"] != slicer_filament:
                slicer_filament = _canon["slicer_filament"]
                # The builtin NAME belonged to the id we just replaced; re-deriving it
                # is the resolver's job, and a stale name would realign the id back.
                slicer_filament_name = None
            if _canon["rgba"]:
                rgba = _canon["rgba"]
            if _canon["nozzle_temp_min"] is not None:
                nozzle_temp_min = _canon["nozzle_temp_min"]
            if _canon["nozzle_temp_max"] is not None:
                nozzle_temp_max = _canon["nozzle_temp_max"]
        # Only a POSITIVE reported net weight overrides the model default.
        label_weight = parsed.label_weight if parsed.label_weight > 0 else None
        source = "tray"
    else:
        material = default_filament.get("material") or "PLA"
        subtype = (default_filament.get("subtype") or "").strip() or None
        color_name = None
        rgba = default_filament.get("rgba")
        brand = default_filament.get("brand") or None
        core_weight = 250
        slicer_filament = default_filament.get("slicer_filament") or None
        slicer_filament_name = None
        # W4: stamp the configured default's canonical nozzle range onto the row so
        # the resolver emits it verbatim (a byte-identical backup-group peer).
        nozzle_temp_min = default_filament.get("nozzle_temp_min")
        nozzle_temp_max = default_filament.get("nozzle_temp_max")
        label_weight = None  # use the Spool model default (1000 g)
        source = "default"

    kwargs: dict = {
        "material": material,
        "subtype": subtype,
        "color_name": color_name,
        "rgba": rgba,
        "brand": brand,
        "core_weight": core_weight,
        "weight_used": 0,
        "slicer_filament": slicer_filament,
        "slicer_filament_name": slicer_filament_name,
        "nozzle_temp_min": nozzle_temp_min,
        "nozzle_temp_max": nozzle_temp_max,
        "data_origin": DATA_ORIGIN,
        "tag_type": None,
    }
    if label_weight is not None:
        kwargs["label_weight"] = label_weight

    spool = Spool(**kwargs)
    # Initialize relationships BEFORE add() to avoid an async lazy load — the
    # SpoolAssignment back_populates resolution runs synchronously (see #612 and
    # ``create_spool_from_tray``).
    spool.k_profiles = []
    spool.assignments = []
    db.add(spool)
    await db.flush()
    logger.info(
        "Auto-minted tagless spool %d: %s %s %s (source=%s, origin=ams_auto)",
        spool.id,
        material,
        subtype or "",
        color_name or "",
        source,
    )
    return spool


# --- assignment helpers -----------------------------------------------------


async def _assign_from_setting(
    db: AsyncSession, spool: Spool, printer_id: int, ams_id: int, tray_id: int, default: dict
) -> SpoolAssignment | None:
    """Bind a setting-minted spool with a fingerprint seeded from the SETTING.

    A bare tray reports an empty tray_type, so an auto_assign_spool-derived
    fingerprint would be empty and re-trip the SpoolBuddy empty-fingerprint
    replay. Seeding fingerprint_color/type from the default filament suppresses
    that and makes the decision table's later fingerprint-match a no-op (so the slot
    resolves KEEP rather than "different filament" on the next push).

    The binding itself (move semantics + the FIFO stamps) belongs to
    ``spool_binding.bind_spool_to_slot``; this wrapper only owns the fingerprint
    seeding.

    Returns the assignment, or **None when the writer REFUSED the bind** — its move
    damper drops a repeated cross-slot move of one spool inside
    ``spool_binding._MOVE_DAMPER_S`` (the 007-H2C flip-flop fix). Callers MUST honour
    None: nothing was written, so pushing a slot config or broadcasting an assignment
    for it would announce a binding that does not exist. A freshly minted row holds no
    other binding and so can never be a MOVE — for those callers this is defence in
    depth, not an expected path.
    """
    return await bind_spool_to_slot(
        db,
        spool,
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        fingerprint_color=default.get("rgba") or "",
        fingerprint_type=default.get("material") or "",
        origin="tagless_setting",
    )


def _config_settling(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """True while a slot's identity is still UNRESOLVED and must not be written to.

    Scope: the gate exists to protect the firmware's post-insert AUTO-READ window, and
    that window only exists while the printer is IDLE. Mid-print insertions are never
    auto-read — no automatic RFID read, possibly not even a presence-bit flip, and no
    retroactive read at FINISH (``bambu-ams-behavior/resources/ams-wire-behavior.md``
    §"Mid-print insertions are not auto-read", live-verified H2S AMS 2 fw 01.01.02.00) —
    so during RUNNING/PAUSE there is no read to clobber and the gate would be pure cost.
    Worse than cost: it would starve
    ``spool_recovery``'s forced bare-tray sweep, whose whole job is enrolling an
    operator-inserted backup spool DURING a jam (printer PAUSEd, slot freshly gained —
    precisely when both arms below are active), and deferring a recoverable state to a
    human is what doctrine rule 1 forbids. An UNREADABLE state is not treated as
    mid-print (``on_error=False``), so an unknown printer keeps the gate ON.

    While idle, two arms, either sufficient:

    * the slot gained presence less than :data:`_CONFIG_SETTLE_S` ago — the firmware's
      own insert-read may still be in flight, and our write would kill it;
    * the farm has an UNANSWERED qualified physical cycle for the slot
      (``ams_presence.identity_unanswered``) less than :data:`_CONFIG_SETTLE_MAX_S`
      old — somebody moved a roll here and nothing has established what it is, so any
      identity we publish is a guess that overwrites the real answer.

    A slot with no recorded gain and no pending question (restart, never observed,
    long-settled) reads as settled, so the gate can never wedge a slot. Distinct from
    :func:`_mint_settling` (F1), which guards phantom MINTS over a much shorter window
    — this one guards the WIRE.

    The second arm CONCLUDES EARLY when the answer it waits for cannot arrive. Waiting
    is only ever worth it while an identity read is possible: an idle printer whose read
    the wire refuses for a cause that will not self-clear
    (``ams_presence.read_unavailable_reason`` — today engaged filament, which needs a
    commanded unload) can produce no answer this epoch, and the firmware's own autonomous
    reads happen at insert/load, so an identity still in flight would already be asserting
    itself on the wire. Sitting out the cap there buys nothing and costs the slot: on
    2026-08-12 an untagged roll inserted into an idle 009-H2S slot was engaged by the AMS
    on the spot, the discovery read was refused for exactly that reason, and the tagless
    mint waited the FULL :data:`_CONFIG_SETTLE_MAX_S` while the queue staged behind it.
    The conclusion is by CAUSE, never by duration (doctrine rule 6) — the cap still owns
    the "no cause, just old" case below — it SPENDS nothing (no occasion, cycle or echo
    state moves: a refusal is not an answer, invariant 13, and the preserved entitlement is
    what lets a later disengaged read correct a mis-defaulted roll), and it pre-approves
    nothing: the write it releases is still re-evaluated at publish time by
    :func:`_push_config` and the client's own refusal (invariant 2). Arm 1 is untouched —
    a slot that gained presence seconds ago still waits out the firmware's insert-read
    window whatever the feeder is doing.
    """
    if _printer_busy(printer_id, on_error=False):
        return False  # mid-print: no firmware auto-read exists to protect
    gain_age = ams_presence.recent_gain_age(printer_id, ams_id, tray_id)
    if gain_age is not None and gain_age < _CONFIG_SETTLE_S:
        return True
    key = (printer_id, ams_id, tray_id)
    if not ams_presence.identity_unanswered(printer_id, ams_id, tray_id):
        _settle_concluded_logged.discard(key)  # episode over: a later one re-announces
        return False
    cycle_age = ams_presence.last_physical_cycle_age(printer_id, ams_id, tray_id)
    reason = ams_presence.read_unavailable_reason(printer_id, ams_id, tray_id)
    if reason is not None:
        if key not in _settle_concluded_logged:
            _settle_concluded_logged.add(key)
            logger.info(
                "[Printer %s] AMS%d slot%d: identity unanswerable this epoch (%s) — "
                "config settle concluded at ~%.0fs instead of the %.0fs cap",
                printer_id,
                ams_id,
                tray_id,
                reason,
                cycle_age or 0.0,
                _CONFIG_SETTLE_MAX_S,
            )
        return False
    return cycle_age is not None and cycle_age < _CONFIG_SETTLE_MAX_S


async def _push_config(db: AsyncSession, spool: Spool, printer_id: int, ams_id: int, tray_id: int, tray: dict) -> bool:
    """Publish the tagless filament config to the slot — the module's ONE wire write.

    The single funnel for every tagless config-write path (first bare-tray mint, bound
    re-push, :func:`_replace_row_after_cycle`, and the pipeline's MINT/pre-config applies
    through :func:`push_config_for_spool`), which is why the settle gate lives here: no path may publish an identity into a
    slot whose own identity is still unresolved (:func:`_config_settling`). Returns
    False without publishing when the slot is settling or the push fails.
    """
    from backend.app.api.routes.inventory import apply_spool_to_slot_via_mqtt

    if _config_settling(printer_id, ams_id, tray_id):
        logger.debug(
            "Deferring tagless config push for printer %d AMS%d-T%d: slot identity still settling",
            printer_id,
            ams_id,
            tray_id,
        )
        return False

    try:
        return await apply_spool_to_slot_via_mqtt(
            db=db,
            current_user=None,
            spool=spool,
            printer_id=printer_id,
            ams_id=ams_id,
            tray_id=tray_id,
            current_tray_info_idx=tray.get("tray_info_idx", "") or "",
            current_tray_type=tray.get("tray_type", "") or "",
        )
    except Exception:  # noqa: BLE001 — log a TRANSIENT push failure; a later AMS push retries it
        # NOT self-healing for a deterministic error: the callee's lazy-load crash
        # (walking spool.k_profiles on a pre-existing DB spool) used to fail EVERY
        # push here and was fixed at apply_spool_to_slot_via_mqtt. What remains is a
        # genuinely transient MQTT/config failure — while the slot stays bare the
        # bare-tray trigger re-fires (gated by _AUTOCONFIG_RETRY_S) so a transient
        # miss is retried. An AMS push only re-fires it while the AMS state HASH
        # keeps changing (bambu_mqtt's anti-storm gate) — the guaranteed lane is the
        # scheduler tick's reconcile_slot_config; a stuck-bare slot eventually
        # escalates via spool_recovery's forced sweep.
        logger.exception(
            "Bare-tray config push failed for spool %d on printer %d AMS%d-T%d",
            spool.id,
            printer_id,
            ams_id,
            tray_id,
        )
        return False


async def push_config_for_spool(
    db: AsyncSession, spool: Spool, printer_id: int, ams_id: int, tray_id: int, tray: dict
) -> bool:
    """Public entry to the module's ONE wire-write funnel (:func:`_push_config`).

    Used by the slot pipeline (W3a) for the two writes it owns — the pre-configured
    one-shot apply and a default-minted row's first identity push — so those go through
    the same settle gate and the same failure handling as every existing config write.
    No logic is duplicated at the call site: this is a delegation, nothing else.
    """
    return await _push_config(db, spool, printer_id, ams_id, tray_id, tray)


async def _broadcast_auto_assigned(
    printer_id: int, ams_id: int, tray_id: int, spool_id: int, origin: str | None = None
) -> None:
    """Broadcast a ``spool_auto_assigned`` slot event.

    ``origin`` distinguishes this module's tagless silent-mint (``"tagless"`` —
    a genuinely NEW untagged roll the frontend toasts about) from the RFID
    auto-assign broadcasts elsewhere (``main.on_ams_change`` / ``routes.inventory``),
    which omit the field. The key is only added when ``origin`` is given so RFID
    payloads stay byte-for-byte unchanged (absent field).
    """
    payload: dict = {
        "type": "spool_auto_assigned",
        "printer_id": printer_id,
        "ams_id": ams_id,
        "tray_id": tray_id,
        "spool_id": spool_id,
    }
    if origin is not None:
        payload["origin"] = origin
    await ws_manager.broadcast(payload)


# --- W1 spent-binding release / fresh-roll transition ----------------------


def _apply_new_fields(spool: Spool, fields: dict | None) -> None:
    """Apply the new-roll route's optional manual fields onto a fresh row.

    Only non-empty values write (a blank field leaves the mint default). Used when
    the operator records a Fresh roll with brand / label weight / cost / note.
    """
    if not fields:
        return
    brand = (fields.get("brand") or "").strip()
    if brand:
        spool.brand = brand
    lw = fields.get("label_weight")
    if lw:
        try:
            spool.label_weight = int(lw)
        except (TypeError, ValueError):
            pass
    cost = fields.get("cost_per_kg")
    if cost is not None:
        spool.cost_per_kg = cost
    note = (fields.get("note") or "").strip()
    if note:
        spool.note = note


async def _replace_row_after_cycle(
    db: AsyncSession,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tray: dict | None,
    departed: Spool,
    *,
    new_fields: dict | None = None,
) -> Spool:
    """Archive a departed tagless row and mint+bind+push its replacement (W1/W5).

    The OPERATOR lane's spent-binding / fresh-roll transition, shared by
    :func:`maybe_autoconfigure_bare_tray` and the W5 new-roll route
    (:func:`apply_fresh_roll` — the "New roll" verb). The WIRE lane's equivalent is the
    pipeline's ``REPLACE_SPENT`` arm (``slot_pipeline._apply_replace_spent``), which
    mirrors this behaviour with one documented difference (it disposes a pristine
    provisional row instead of archiving an empty husk); the two must stay aligned.
    Default-mints from the configured tagless default
    when the tray is bare/absent OR still carries the departed row's config (firmware
    leftover — :func:`fingerprint_matches`), so a physically-fresh roll gets the fleet
    default's complete identity; else mints from the tray's own (genuinely different)
    config.
    Optional ``new_fields`` (brand/label_weight/cost_per_kg/note) ride the new row.
    Commits; broadcasts ``spool_auto_assigned(origin="tagless")``. Returns the new spool.
    """
    departed.archived_at = datetime.utcnow()  # keep the ledger row + its grams
    res = await db.execute(
        select(SpoolAssignment).where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    old = res.scalar_one_or_none()
    if old is not None:
        await db.delete(old)
        await db.flush()

    default = await _tagless_default(db)
    tray_configured = bool(tray and (tray.get("tray_type") or "").strip())
    use_default = default is not None and (not tray_configured or fingerprint_matches(departed, tray))

    if use_default:
        new_spool = await mint_tagless_spool(db, default_filament=default)
        _apply_new_fields(new_spool, new_fields)
        bound = await _assign_from_setting(db, new_spool, printer_id, ams_id, tray_id, default)
        await db.commit()
        if bound is None:
            # The writer refused the bind (move damper). Nothing claims the slot, so
            # publishing a config for it — or announcing an assignment — would be a lie.
            logger.warning(
                "Tagless replacement spool %d was not bound to printer %d AMS%d-T%d (writer refused) — "
                "skipping the slot config push",
                new_spool.id,
                printer_id,
                ams_id,
                tray_id,
            )
            return new_spool
        await _push_config(db, new_spool, printer_id, ams_id, tray_id, tray or {})
    else:
        if not tray_configured:
            # No configured tray to mint from and no default → cannot build an identity.
            raise ValueError("cannot replace tagless row: no tagless default and tray is not configured")
        new_spool = await mint_tagless_spool(db, tray=tray)
        _apply_new_fields(new_spool, new_fields)
        await auto_assign_spool(
            printer_id,
            ams_id,
            tray_id,
            new_spool,
            printer_manager,
            db,
            tray_info_idx=tray.get("tray_info_idx", "") or "",
        )
        await db.commit()
    await _broadcast_auto_assigned(printer_id, ams_id, tray_id, new_spool.id, origin="tagless")
    return new_spool


# --- ledger-overcharge reconcile (009-H2S spool 290, 2026-08-12) ------------
#
# A tagless row can only ever be re-bound to its slot on IDENTITY-FREE evidence (a
# ``last_location_*`` breadcrumb, a fingerprint match): there is no chip to say whether the
# roll that came back is the roll that left. 009-H2S slot 2 took a swap on that evidence on
# 2026-08-12: spool 290 was re-bound after a 23-minute absence, absorbed the NEW roll's
# prints, reached 1200.48 g used on a 1000 g label — physically impossible — and so read
# ``remaining_g == 0``, failed the 150 g start floor and staged the whole production run
# silently for six hours behind a row describing filament that no longer existed.
#
# THE HEADER THIS REPLACES ASSERTED THE OPPOSITE, AND THAT ASSERTION COST THREE SESSIONS.
# It read: the reclaim is "doctrine-CORRECT — rule 7 forbids any duration threshold from
# deciding tagless identity … refusing to reclaim would break the far more common case."
# Both clauses are superseded (2026-08-19, operator-ratified — shape 32, whose
# fourth-attempt table shows each of the three prior rejections of a bounded window
# immediately preceding an incident of this class):
#
#  * **Rule 7 IS AMENDED.** Duration still decides no tagless identity POSITIVELY; a
#    bounded window decides the NEGATIVE — outside it the farm asserts nothing and MINTS.
#    The prohibition on timing-based novelty is replaced by a prohibition on UNBOUNDED,
#    CAUSE-BLIND breadcrumb reclaim. The lane survives only as a de-bounce for a SPURIOUS
#    release: the slot's single last occupant, untagged, absence MEASURED and inside
#    ``ams_presence._RESEAT_WINDOW_S``, and disqualified BY CAUSE whenever the release has
#    a physical explanation — a runout, a mid-print pull (``slot_pipeline
#    ._debounce_candidate`` / ``_runout_suspect``).
#  * **"The far more common case" was finally MEASURED, and it is not the case that
#    sentence meant.** Of 52 reclaims with a matched prior release over 8 days, 14 returned
#    inside five minutes — four at 0.0 min across four printers inside ONE minute, which no
#    human performs — and those are spurious releases the lane was silently repairing. The
#    other 36 spanned 23 min to 3.3 days and were information-free guesses. The gap
#    distribution has an empty valley between 1.7 min and 23 min, so the boundary is
#    confirmed by data rather than chosen, and the common case is now correctly served
#    rather than used as the argument for serving nothing correctly.
#
# What that leaves for THIS lane is stated below and is unchanged in kind — but note its
# reach has narrowed by design: a de-bounce no longer stamps a re-bind boundary at all
# (``slot_pipeline._debounce_bind_moment`` carries the incumbent moment forward, because
# the farm has just certified that no physical event happened), so the surviving reachable
# trigger is an operator manually assigning an old row to a slot holding a new roll.
#
# This lane is NOT a second guess at the de-bounce. It fires only once the ledger has become
# PROVABLY impossible AND a known re-bind moment exists to attribute across. Two
# independent facts are required and neither is sufficient alone:
#
#  * **Overshoot** past the label by more than :data:`_OVERCHARGE_MARGIN_G`. Alone this
#    proves nothing: manufacturers overfill (some ship ~1100 g on a 1000 g label), so a
#    row bound CONTINUOUSLY since it entered service gets no action and — by operator
#    ruling — not even a warning, because a warning there is noise. Rule 8 governs it:
#    hardware runout evidence is the exhaustion truth, never ``label − weight_used``.
#  * **A recorded re-bind.** A release HARD-DELETES the assignment row, so a live
#    ``SpoolAssignment.created_at`` is the moment of the CURRENT bind. Meaningfully later
#    than the spool row's own ``created_at`` means the roll left and came back at a KNOWN
#    instant — the only instant at which an unobserved swap could have happened.
#
# Attribution is then EXACT, never apportioned: the successor is charged the SUM of the
# ``spool_usage_history`` rows recorded since that boundary, and those rows are RE-POINTED
# to it (double entry — the old row is decremented by the same sum and archived). If the
# swap actually happened at an EARLIER, unobserved cycle the successor is UNDER-charged,
# which is the optimistic direction and the only admissible error: a roll that reads
# fuller than it is simply runs out, and the runout machinery corrects it (rule 8). No
# path here may ever over-charge the successor.
#
# TAGGED rows are never touched. Their identity is tag-bound, so an overshoot there is not
# a swap story at all — it is misattribution evidence to root-cause by hand (rule 10: no
# tag has ever been reused on this farm). They get one WARNING per row per re-notify
# window and nothing else.

# Grams past the label that still count as ordinary overfill + attribution slack rather
# than an impossible ledger. ≈2 % of a 1 kg label — wide enough for the vendor overfill
# the operator ruling names, and one layer-segment attribution quantum on top (the
# ``usage_tracker`` charges a print's grams to feeders in ``tray_change_log`` segments, so
# a single boundary segment can land a handful of grams on either side of the truth).
# Deliberately a code constant, not a setting: it is derived from how the hardware and the
# tracker behave, and nothing an operator could tune it to would be more correct.
_OVERCHARGE_MARGIN_G = 20.0

# The MIRROR margin, for the backward direction (:func:`reattribute_early_runout`). It
# answers a different question from the one above and so gets its own derivation rather
# than the same number by habit — conflating two margins is how a safety direction gets
# inverted without anyone noticing.
#
# The comparison here is "does what the successor DELIVERED account for what the departed
# row still had on the books": two farm-accumulated ``usage_tracker`` ledgers, read across
# one assumed label. The only slack in that which is genuinely NOISE is the tracker's
# layer-segment attribution quantum — "a handful of grams on either side of the truth", as
# the margin above puts it — once per ledger, so roughly two quanta. 20 g, 2 % of a 1 kg
# label.
#
# **Vendor overfill is deliberately NOT budgeted for here, unlike above.** Same physical
# fact, opposite side of the safety line: up there a WIDER margin means FEWER reconciles
# (silence, the safe direction), while a wider margin here means MORE merges — and a merge
# that is wrong writes two physical rolls into one ledger row, permanently. Operator
# rulings 4 and 5 genuinely conflict in that band, because a DIFFERENT part-used roll can
# deliver roughly what the departed row had left, and the band belongs to ruling 4: a
# different roll, stand down, log it (scenario C4). The acting arm is an optimisation; the
# stand-down arm is the feature. So a roll that really was overfilled fails the fit test
# and is simply not corrected — a missed correction self-heals at the next runout, a wrong
# one never does (cross-cutting invariant 11's asymmetry).
#
# A code constant and not a setting, for :data:`_OVERCHARGE_MARGIN_G`'s reason: it is
# derived from how the tracker attributes grams, and nothing an operator could tune it to
# would be more correct.
_REATTRIBUTION_FIT_MARGIN_G = 20.0

# How much later than the spool row's own creation an assignment's ``created_at`` must sit
# before it counts as a RE-bind rather than the original one. A mint and its bind happen
# inside one request — the gap is milliseconds to seconds (a mint, a flush, a bind, a
# commit) — while the re-bind this lane looks for is separated from the mint by at least
# one print. Two minutes is far above the former and far below the latter, so the boundary
# test needs no other tuning.
_RECONCILE_BOUNDARY_EPSILON_S = 120.0

# Durable notify-ledger scope for the TAGGED warn-once branch. Its own key space (keyed by
# spool, like the spent-contradiction scope beside it) so neither lane's window can be
# governed by the other's prune or dedup.
_LEDGER_OVERCHARGE_SCOPE = "ledger_overcharge"

# Fields the successor inherits from the row it replaces: what the physical filament IS,
# what it costs, and where inventory files it. Everything NOT listed is deliberately reset
# — the grams ledger (the successor gets exactly the re-pointed charges), the tag columns
# (a tagless mint has none), the spent/prompt/fault stamps (they described the old roll)
# and the FIFO stamps (``spool_binding`` owns those, and the successor's service entry
# genuinely IS this boundary).
_SUCCESSOR_INHERITED_FIELDS = (
    "material",
    "subtype",
    "color_name",
    "rgba",
    "extra_colors",
    "effect_type",
    "brand",
    "slicer_filament",
    "slicer_filament_name",
    "nozzle_temp_min",
    "nozzle_temp_max",
    "label_weight",
    "core_weight",
    "core_weight_catalog_id",
    "cost_per_kg",
    "category",
    "low_stock_threshold_pct",
    "storage_location",
    "location_id",
)


async def _mint_successor_row(db: AsyncSession, departed: Spool, *, weight_used: float) -> Spool:
    """Mint the replacement row for a reconciled overcharge — same filament, fresh ledger.

    NOT :func:`mint_tagless_spool`: that helper DERIVES an identity from a tray dict or the
    tagless-default setting, and neither is the right source here. The physical roll in the
    slot is the one whose grams we are re-attributing, so its identity is exactly the
    departed row's (:data:`_SUCCESSOR_INHERITED_FIELDS`) — re-deriving from the default
    would silently re-brand a roll the operator had corrected. Field parity with the mint
    is otherwise kept verbatim: no tag columns, ``data_origin = ams_auto``,
    ``tag_type = None``, and relationships initialised BEFORE ``add()`` to avoid an async
    lazy load (#612).
    """
    kwargs = {name: getattr(departed, name) for name in _SUCCESSOR_INHERITED_FIELDS}
    successor = Spool(**kwargs, weight_used=weight_used, data_origin=DATA_ORIGIN, tag_type=None)
    # The charges moving across are prints this physical roll actually fed, so the
    # feed-evidence field moves with them (rule 8 names ``last_used`` as the field that
    # survives repairs and proves a roll fed).
    successor.last_used = departed.last_used
    successor.k_profiles = []
    successor.assignments = []
    db.add(successor)
    await db.flush()
    return successor


async def _warn_tagged_overcharge(db: AsyncSession, spool: Spool, assignment: SpoolAssignment) -> None:
    """One WARNING per tagged over-charged row per re-notify window. Never mutates.

    A tagged row's identity is RFID, so the tagless swap story does not apply to it and an
    automatic reconcile would destroy the evidence: by rule 10 no tag has ever been reused
    on this farm, which makes an impossible ledger on a tagged row a MISATTRIBUTION to
    root-cause, not a roll change to book. Log-only and deliberately NOT a notification —
    the operator ruling for this whole lane is that the log line is the only surface.

    Paced through the durable notify ledger (``notify_dedup``) rather than an in-memory
    gate for the same reason the spent-contradiction detector is: the condition re-derives
    identically on every pass forever, so an in-memory window would re-blast at every
    deploy. Shares that lane's :data:`spool_respool._SPENT_CONTRADICTION_RENOTIFY_S`
    window — one origin for "how often a STANDING ledger complaint may re-warn".
    """
    from backend.app.services import notify_dedup

    key = f"spool:{spool.id}"
    last = await notify_dedup.last_sent_at(db, _LEDGER_OVERCHARGE_SCOPE, key)
    if last is not None and (datetime.utcnow() - last).total_seconds() < (
        spool_respool._SPENT_CONTRADICTION_RENOTIFY_S
    ):
        return
    logger.warning(
        "[RESPOOL] LEDGER-OVERCHARGE (TAGGED, NOT RECONCILED): spool %d reads %.0fg used on a %.0fg label "
        "at printer %d AMS%d-T%d, re-bound %s after the row was created %s. An RFID row's identity is "
        "tag-bound and no tag has ever been reused on this farm, so this is misattribution evidence to "
        "root-cause by hand — nothing was changed.",
        spool.id,
        float(spool.weight_used or 0.0),
        float(spool.label_weight or 0.0),
        assignment.printer_id,
        assignment.ams_id,
        assignment.tray_id,
        assignment.created_at.isoformat() if assignment.created_at else "?",
        spool.created_at.isoformat() if spool.created_at else "?",
    )
    await notify_dedup.record_sent(db, _LEDGER_OVERCHARGE_SCOPE, key)


async def _charges_since(db: AsyncSession, spool_id: int, boundary: datetime) -> float:
    """Grams charged to ``spool_id`` at or after ``boundary``. One origin, two directions."""
    return float(
        (
            await db.execute(
                select(func.sum(SpoolUsageHistory.weight_used)).where(
                    SpoolUsageHistory.spool_id == spool_id,
                    SpoolUsageHistory.created_at >= boundary,
                )
            )
        ).scalar()
        or 0.0
    )


async def _repoint_charges(db: AsyncSession, *, from_spool_id: int, to_spool_id: int, boundary: datetime) -> None:
    """Move every usage-history row at or after ``boundary`` from one spool row to another.

    Charge re-attribution is ONE lane with TWO directions, and this is the half both share
    (house rule: one canonical implementation per feature). FORWARD — a swap the farm
    swallowed, so grams charged since the boundary belong to a SUCCESSOR
    (:func:`replace_bound_row_with_successor`). BACKWARD — a roll change the farm invented,
    so those grams go back to the PREDECESSOR that never actually left
    (:func:`replace_bound_row_with_predecessor`). Same double entry, opposite evidence;
    the callers own the ``weight_used`` arithmetic and the disposal because those genuinely
    differ.
    """
    await db.execute(
        update(SpoolUsageHistory)
        .where(SpoolUsageHistory.spool_id == from_spool_id, SpoolUsageHistory.created_at >= boundary)
        .values(spool_id=to_spool_id)
    )


async def _hand_charges_back(db: AsyncSession, *, row: Spool, predecessor: Spool, boundary: datetime) -> float:
    """Move ``row``'s post-boundary charges back onto ``predecessor``. Returns the grams moved.

    The BACKWARD half of the one re-attribution lane, shared by both of its triggers so the
    double-entry arithmetic — which ledger gains, which loses, and where the clamp goes —
    has exactly one body (house rule: one canonical implementation per feature). The two
    triggers hold different evidence for the same conclusion, "the row change never
    happened":

    * :func:`replace_bound_row_with_predecessor` — a HUMAN says so (doctrine rule 12's
      acknowledgement undo, scenario R8), and the predecessor goes back into the slot;
    * :func:`reattribute_early_runout` — the HARDWARE says so, by running the successor out
      on grams that only add up as the predecessor's remainder (scenario C3), and nothing
      is re-bound because the slot is empty.

    Disposal, re-binding and any stamp a caller carries across stay OUT of here: those are
    what genuinely differ between the two, and folding them in would make this function
    mean two things. Zero grams writes nothing at all — with no per-print provenance to
    move there is no attribution to make, the same refusal the forward direction encodes as
    ``require_positive_moved``. Flush-free; the callers own both the flush and the commit.
    """
    moved = await _charges_since(db, row.id, boundary)
    if moved <= 0:
        return 0.0
    await _repoint_charges(db, from_spool_id=row.id, to_spool_id=predecessor.id, boundary=boundary)
    predecessor.weight_used = float(predecessor.weight_used or 0.0) + moved
    row.weight_used = max(0.0, float(row.weight_used or 0.0) - moved)
    return moved


async def replace_bound_row_with_predecessor(
    db: AsyncSession,
    row: Spool,
    predecessor: Spool,
    boundary: datetime,
    *,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    fingerprint_color: str | None,
    fingerprint_type: str | None,
    origin: str,
) -> float:
    """Retract a mint: hand its grams back to the row it displaced and re-bind that row.

    The MIRROR of :func:`replace_bound_row_with_successor`, in the same module and over the
    same re-pointing primitive (:func:`_repoint_charges`), because they are one concept in
    two directions rather than two lanes. Forward evidence is "the charges themselves prove
    a swap"; backward evidence is "a human says the swap never happened" — doctrine rule 12's
    acknowledgement, whose whole reason to exist is that an operator re-checking a slot they
    merely re-seated gets an HONEST false positive with a one-click exit (scenario R8). The
    HARDWARE can say the same thing (:func:`reattribute_early_runout`, scenario C3), which
    is why the double entry itself lives in the shared :func:`_hand_charges_back` and only
    the disposal and the re-bind are stated here.

    Sequence, one unit of work: SUM the grams charged to ``row`` at or after ``boundary``,
    re-point those history rows to ``predecessor``, add the sum back to its ``weight_used``,
    zero the retracted row's share, dispose it through the fork's canonical disposal
    (``dispose_provisional_on_tag`` — a pristine auto-minted row is hard-deleted, a
    ledger-bearing one archived) and re-bind ``predecessor`` to the slot through the ONE
    binding writer.

    ``preserve_ordinal=True``: the predecessor is resuming a service life that never ended,
    so re-stamping ``loaded_at`` would move a roll to the back of the FIFO queue for a
    binding change the operator has just declared did not happen (doctrine rule 7 — a
    mid-life re-seat keeps its position).

    ``archived_at`` is cleared when the retracted mint's own path set it — an archived row
    must never hold a location claim (table row 4e would replace it on the next push, which
    is the undo undoing itself). ``spent_at`` is deliberately NOT cleared: that is hardware
    evidence, and the operator ruling of 2026-08-09 forbids an un-spend lane outright. A
    restored spent row therefore lands back in exactly the parked state the click found,
    which is what "restore the previous roll" means.

    Returns the grams moved back. Flush-only — the caller owns the commit, matching
    ``bind_spool_to_slot``'s contract and this module's other re-attribution entry point.
    """
    moved = await _hand_charges_back(db, row=row, predecessor=predecessor, boundary=boundary)

    predecessor.archived_at = None
    disposition = await dispose_provisional_on_tag(db, row)
    if disposition == "kept":
        row.archived_at = datetime.utcnow()
    await db.flush()

    await bind_spool_to_slot(
        db,
        predecessor,
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        fingerprint_color=fingerprint_color,
        fingerprint_type=fingerprint_type,
        origin=origin,
        preserve_ordinal=True,
    )
    logger.info(
        "[tagless] printer=%d A%dT%d re-check mint spool %d retracted (%s); spool %d restored with %.1f g handed back",
        printer_id,
        ams_id,
        tray_id,
        row.id,
        disposition,
        predecessor.id,
        moved,
    )
    return moved


async def replace_bound_row_with_successor(
    db: AsyncSession,
    spool: Spool,
    assignment: SpoolAssignment,
    boundary: datetime,
    *,
    stamp_donor_spent: bool = False,
    require_positive_moved: bool = True,
    origin: str = "ledger_reconcile",
) -> Spool | None:
    """Retire the row bound to a slot and hand that slot a successor carrying its grams.

    THE state change behind "the physical roll in this slot changed at ``boundary``, so
    everything charged since then belongs to the NEW one". Double entry, in one unit of
    work: SUM the ``spool_usage_history`` rows at or after ``boundary``, mint the successor
    carrying exactly that sum, RE-POINT those history rows to it, decrement the departing
    row by the same sum (clamped at 0) and archive it — it left service at the boundary.
    Returns the successor, or None when nothing was written.

    Two callers hold two different kinds of evidence for the same swap, which is what the
    three keywords select between; the mechanics are identical, so they live here once:

    * :func:`_reconcile_one_overcharge` (all defaults) — the ledger-overcharge sweep, where
      the CHARGES THEMSELVES are the proof. Nothing to move ⇒ nothing proved, so
      ``require_positive_moved`` returns None having written nothing, and the overshoot
      stays somebody else's evidence.
    * The one-time ``repair_runout_reclaim_20260813`` migration in ``core.database``, where
      the OPERATOR'S RULING is the evidence (a runout-dead row resurrected onto a fresh
      roll by ``last_location_reclaim``). There a zero-gram move is still a real roll change
      to book — a print held on the runout charged nothing yet — hence
      ``require_positive_moved=False``; and the departing row gets ``spent_at`` because it
      physically ran dry. That last one is a direct write outside
      ``spool_respool._mark_tray_spent``, the single live spent writer, and it is the
      **sanctioned repair exception** (plan ``003-h2s-ran-out-of-eventual-harbor.md`` WS6):
      a repair asserts a fact about a past the wire will never re-assert, so routing it
      through a lane that resolves LIVE tray state would have nothing to resolve.

    The successor is bound through ``spool_binding.bind_spool_to_slot``, the ONE binding
    writer, which displaces the departed row's claim with its own INFO trail and
    ``last_location_*`` stamp instead of a hand-rolled delete. The FIFO ordinal is left to
    re-stamp normally (no ``preserve_ordinal``): the successor's service entry genuinely IS
    this boundary — a different row is now bound to the slot, which is precisely the
    binding change rule 7 re-stamps on. A fresh row holds no other binding, so the writer's
    move damper cannot refuse the call.

    **No AMS config is pushed** — deliberately, unlike :func:`_replace_row_after_cycle`,
    which is a physical roll-change lane and must give the firmware an identity for a slot
    whose contents just changed. Here the tray's wire config is already correct: the
    physical roll never changed at this instant (the swap happened at the boundary, hours
    ago) and the successor inherits the departed row's filament identity byte for byte, so
    a write could only re-state what the slot already carries — and every avoided AMS write
    is one less chance to split the firmware's auto-refill backup group.

    Flush-only — the caller owns the commit, matching ``bind_spool_to_slot``'s contract and
    both call sites' transaction shapes (the sweep's session; the migration's savepoint).
    """
    moved = await _charges_since(db, spool.id, boundary)
    if moved <= 0 and require_positive_moved:
        return None

    # One clock reading for both stamps: the row left service and ran dry at the same
    # instant as far as this repair is concerned.
    now = datetime.utcnow()
    successor = await _mint_successor_row(db, spool, weight_used=moved)
    await _repoint_charges(db, from_spool_id=spool.id, to_spool_id=successor.id, boundary=boundary)
    spool.weight_used = max(0.0, float(spool.weight_used or 0.0) - moved)
    spool.archived_at = now
    if stamp_donor_spent:
        spool.spent_at = now

    await bind_spool_to_slot(
        db,
        successor,
        printer_id=assignment.printer_id,
        ams_id=assignment.ams_id,
        tray_id=assignment.tray_id,
        fingerprint_color=assignment.fingerprint_color,
        fingerprint_type=assignment.fingerprint_type,
        origin=origin,
    )
    return successor


async def _reconcile_one_overcharge(
    db: AsyncSession, spool: Spool, assignment: SpoolAssignment, boundary: datetime
) -> bool:
    """The overcharge sweep's lane over :func:`replace_bound_row_with_successor`.

    Contributes exactly what is this lane's own and not the shared mechanism's: the
    commit (the sweep runs candidate-by-candidate, each ending in its own COMMIT or
    ROLLBACK) and the narrative WARNING that names the impossible ledger. Returns False,
    having written nothing, when there were no post-boundary charges to move — with no
    charges there is no attribution to make and the overshoot is somebody else's evidence.
    """
    before_used = float(spool.weight_used or 0.0)
    # Read off the assignment BEFORE the replace: the bind deletes that row and the commit
    # detaches it, while the log below runs after both.
    printer_id, ams_id, tray_id = assignment.printer_id, assignment.ams_id, assignment.tray_id
    successor = await replace_bound_row_with_successor(db, spool, assignment, boundary)
    if successor is None:
        return False

    # Every history row now pointing at the brand-new successor is one this call moved, so
    # the narrative's charge count derives from the result rather than re-scanning the
    # boundary predicate the mechanism already applied.
    charges = int(
        (
            await db.execute(select(func.count(SpoolUsageHistory.id)).where(SpoolUsageHistory.spool_id == successor.id))
        ).scalar()
        or 0
    )
    moved = float(successor.weight_used or 0.0)
    await db.commit()

    logger.warning(
        "[RESPOOL] LEDGER-OVERCHARGE RECONCILED: tagless spool %d read %.0fg used on a %.0fg label at "
        "printer %d AMS%d-T%d; re-bind boundary %s — moved %.0fg (%d charges) to successor spool %d, "
        "old row archived at %.0fg used. Runout evidence remains the exhaustion truth.",
        spool.id,
        before_used,
        float(spool.label_weight or 0.0),
        printer_id,
        ams_id,
        tray_id,
        boundary.isoformat(),
        moved,
        charges,
        successor.id,
        float(spool.weight_used or 0.0),
    )
    return True


async def reconcile_ledger_overcharges(db: AsyncSession) -> int:
    """Reconcile every spool row whose gram ledger has become physically impossible.

    See the section comment above for the incident and the two-fact proof. Candidates are
    bound, un-archived, non-spent rows with a positive label reading more than
    :data:`_OVERCHARGE_MARGIN_G` past it; a row with no re-bind boundary is skipped in
    total silence (operator ruling: overfill is not news), a TAGGED row gets
    :func:`_warn_tagged_overcharge` and nothing else, and a tagless row with a boundary is
    reconciled by :func:`_reconcile_one_overcharge`. Returns how many rows it reconciled.

    Rides the spent-contradiction detector's throttle and Spoolman gate — it is invoked
    from inside ``spool_respool.detect_spent_contradictions``, so there is ONE throttled
    ledger-integrity entry per reconcile tick rather than two competing floors, and
    Spoolman installs (where the spool lifecycle is not ours to rewrite) never reach it.
    The sweep lives HERE, beside :func:`_replace_row_after_cycle`, because it needs this
    module's mint + bind mechanics and ``spool_respool`` cannot import them: this module
    already imports ``spool_respool`` at module scope, so the dependency only runs the
    other way as a deferred call.

    Two accepted residuals, both documented rather than defended against:

    * A print RUNNING during the reconcile may charge its terminal grams to the ARCHIVED
      old row, because ``usage_tracker`` resolves the charged spool from a snapshot taken
      at print START. Those grams then miss the successor — the optimistic direction
      again, corrected by the runout backstop, and the alternative (blocking the reconcile
      on an idle fleet) would leave the run staged for as long as the print lasts.
    * ``SpoolUsageHistory`` is TRACKER-written only: an operator's scale weigh-in or a
      manual ``weight_used`` edit never appears in it. That is exactly why ``moved`` is
      summed from the history rows instead of taken as a ``weight_used`` delta — the sum
      is the part of the ledger with per-print provenance to move, and a manual adjustment
      the operator made to the old row is left on the old row, where they made it.

    FULLY self-guarding (invariant 10), per candidate and around the whole sweep: this is
    an entry hook, so nothing it does may reach the tick that hosts it.
    """
    try:
        return await _scan_ledger_overcharges(db)
    except Exception:  # noqa: BLE001 — an entry hook owns its guard; the tick must survive
        logger.exception("Ledger-overcharge reconcile failed (non-fatal)")
        return 0


async def _scan_ledger_overcharges(db: AsyncSession) -> int:
    """The sweep :func:`reconcile_ledger_overcharges` guards. See it.

    Candidates are collected as bare IDs and each is re-loaded inside its own iteration,
    never held as ORM rows across the loop. Every candidate ends in a COMMIT or a
    ROLLBACK, and a rollback expires every object in the session — so an entity loaded up
    front would lazy-load (and, on an async session, raise ``MissingGreenlet``) the moment
    the next iteration touched it. The re-read also makes each candidate see the state the
    previous one committed.
    """
    result = await db.execute(
        select(SpoolAssignment.id, SpoolAssignment.spool_id)
        .join(Spool, SpoolAssignment.spool_id == Spool.id)
        .where(
            Spool.archived_at.is_(None),
            Spool.spent_at.is_(None),
            Spool.label_weight > 0,
            Spool.weight_used > Spool.label_weight + _OVERCHARGE_MARGIN_G,
        )
    )
    reconciled = 0
    for assignment_id, spool_id in result.all():
        assignment = await db.get(SpoolAssignment, assignment_id)
        spool = await db.get(Spool, spool_id)
        if assignment is None or spool is None or assignment.spool_id != spool.id:
            continue  # the binding moved under us since the candidate query
        try:
            boundary = assignment.created_at
            created = spool.created_at
            if boundary is None or created is None:
                continue  # no timestamps, no provable boundary
            if (boundary - created).total_seconds() <= _RECONCILE_BOUNDARY_EPSILON_S:
                continue  # bound continuously since it entered service: overfill, not a swap
            if not is_tagless_spool(spool):
                await _warn_tagged_overcharge(db, spool, assignment)
                continue
            if await _reconcile_one_overcharge(db, spool, assignment, boundary):
                reconciled += 1
        except Exception:  # noqa: BLE001 — one bad row must not abort the sweep
            logger.exception(
                "Ledger-overcharge reconcile failed for spool %s on printer %s AMS%s-T%s",
                spool.id,
                assignment.printer_id,
                assignment.ams_id,
                assignment.tray_id,
            )
            try:
                await db.rollback()  # a half-applied candidate must not ride the next one's commit
            except Exception:  # noqa: BLE001 — a dead session cannot be rescued here
                logger.exception("Ledger-overcharge rollback failed")
                return reconciled
    return reconciled


# --- the BACKWARD direction: an early runout hands the charges back ---------
#
# Scenarios C3/C4, and the price of doctrine rule 7's 2026-08-19 amendment rather than a
# nicety on top of it. Scoping the breadcrumb reclaim to a 5-minute glitch filter made the
# farm MINT a fresh row at label weight for every longer absence. That is right for a
# genuine roll change (T2/T3 — the 002/005-H2S incident) and WRONG for a roll pulled for an
# external dry, a jam clear, a tangle fix or an inspection and returned later (T5), and for
# a restart that lands while the roll is out (T11). Those write an ASSUMED full roll over a
# part-used one, which OVER-PROMISES the ``min_start_spool_g`` start gate — the direction
# the operator named critical, because it starts prints that die instead of staging them.
# The amendment's own text accepts that cost only on the condition stated here: it
# "self-corrects at that roll's next runout rather than persisting silently".
#
# This lane is that correction, and it acts at the only moment the hardware states the
# truth. A row that ran dry having delivered far less than its assumed label is one of two
# things: a part-used roll an operator legitimately seated as full (scenario C2 — ordinary
# stock, operator ruling 4, nothing to do), or the farm's own mistaken mint, in which case
# the grams it delivered ARE the remainder still on the books of the row it displaced. When
# those add up the charges go back and the mint is retired (C3); when they do not, nothing
# happens and the reason is logged (C4).
#
# **Why this may act at all, given cross-cutting invariant 11.** Assumption-tier evidence
# may displace NOTHING a live binding holds, and an arithmetic fit is assumption-tier — a
# coincidence-sized argument, not an identity read. The escape is a timing FACT, not an
# exemption: the AMS clears a drained slot's exist bit ~3 MINUTES BEFORE it declares the
# runout (incident shape 31, timed 3× on 2026-08-13), so by the time a spent stamp exists
# the release has already hard-deleted the assignment. Both rows this lane adjudicates are
# therefore DEAD — unbound, out of service — and it displaces nothing.
#
# That is ENFORCED, not assumed — by THIS lane, explicitly. ``last_released_from_slot_stmt``
# used to carry ``~Spool.assignments.any()`` and the enforcement was borrowed from it; that
# filter is gone (a SQL eligibility test does not skip an ineligible row, it makes the row
# invisible and silently answers with an OLDER residue — a different physical roll), so both
# rows are now checked HERE with ``spool_binding.bound_elsewhere``. The lane additionally
# requires the spent row to be the NEWEST row the query returns, so a spent stamp landing
# while the binding is still live finds no candidate and stands down. **If a future change
# ever makes spent stamping fire while the binding is live, this exemption evaporates with
# it** — the stand-down is what keeps that change safe, so it must never be "fixed" into a
# lookup that ignores the assignment.
#
# Error direction, as everywhere else in this module: only ever the optimistic one. Grams
# that do not fit are left exactly where they are. A missed correction self-heals at that
# roll's next runout; a wrong one is permanent.


async def reattribute_early_runout(
    db: AsyncSession,
    spent: Spool,
    *,
    printer_id: int,
    ams_id: int,
    tray_id: int,
) -> Spool | None:
    """Entry hook for the backward correction; returns the row the charges went back to.

    See the section comment above. Called from ``spool_respool._mark_tray_spent`` — THE one
    spent writer — immediately after it commits a NEW stamp, so every one of the four
    trigger lanes reaches this once and none of them reaches it twice: a duplicate trigger
    returns from that function's idempotent branch and never gets here.

    FULLY self-guarding (cross-cutting invariant 10). The spent stamp is already durable
    when this runs, and a correction that is by construction optional must never be able to
    take that stamp — or the MQTT callback chain hosting it — down with it.
    """
    try:
        return await _reattribute_early_runout(db, spent, printer_id=printer_id, ams_id=ams_id, tray_id=tray_id)
    except Exception:  # noqa: BLE001 — an entry hook owns its guard; the stamp above must survive
        logger.exception("Early-runout re-attribution failed (non-fatal)")
        try:
            await db.rollback()  # a half-applied re-point must not ride somebody else's commit
        except Exception:  # noqa: BLE001 — a dead session cannot be rescued here
            logger.exception("Early-runout re-attribution rollback failed")
        return None


async def _reattribute_early_runout(
    db: AsyncSession, spent: Spool, *, printer_id: int, ams_id: int, tray_id: int
) -> Spool | None:
    """The adjudication and the write that :func:`reattribute_early_runout` guards. See it.

    Three questions, in cost order, each answered before the next is asked:

    1. **Is there a mistake to correct?** Only for a row the FARM minted
       (:data:`DATA_ORIGIN` — an operator's own inventory record is never retracted by a
       detector, the same boundary :func:`dispose_provisional_on_tag` draws), untagged
       (doctrine rule 10 — a tagged row's identity is factual, and an over- or under-spend
       there is misattribution to root-cause by hand, never a roll change to book), that ran
       out having delivered less than its assumed label by more than
       ``spool_respool._SPENT_DELIVERY_GAP_NOTE_G``. That floor is deliberately NOT a second
       constant: it is the very gap the spent stamp already RECORDS as this roll's true
       capacity (operator ruling 18), so "the delivery was worth a note" and "the delivery
       was worth checking" stay one threshold with one origin. A roll that delivered about a
       full label was minted correctly and there is nothing here to recover.
    2. **Which row could it have displaced?** EXACTLY ONE — the slot's single last departed
       roll, taken immediately ahead of the one that just ran out (rule 7's amendment:
       "bounded to the slot's single last occupant"). Read through
       ``spool_binding.last_released_from_slot_stmt``, the ONE origin for that question, and
       adjudicated wherever it leads. This lane never scans past it hunting for a row that
       fits — that discipline is the whole difference between an adjudication and a search
       through noise, and it is the same refusal ``_mark_tray_spent`` tier 2 makes.
    3. **Do the grams add up?** The successor's ``Spool.delivered_g`` — the derivation that
       exists for exactly this consumer — against the departed row's ``Spool.remaining_g``,
       the one gram origin per side since 2026-08-12. Inside
       :data:`_REATTRIBUTION_FIT_MARGIN_G` the physical roll never changed; outside it a
       DIFFERENT part-used roll delivered what it delivered (operator ruling 4) and nothing
       is written.

    The write, when all three answer yes: the successor's per-print charges go back through
    the shared :func:`_hand_charges_back`; the departed row inherits the runout's own
    ``spent_at`` INSTANT — re-attributed, never re-stamped off a fresh clock, because one
    runout happened and ``_mark_tray_spent`` remains its sole author. That stamp is not
    decoration either: it is what removes the departed row from the de-bounce donor query
    and from ``spool_tag_matcher.find_matching_untagged_spool`` (both filter
    ``spent_at IS NULL``), so the roll that actually ran dry cannot be reclaimed onto the
    next fresh one — the resurrection half of incident shape 31.

    The mistaken row is ARCHIVED rather than disposed, unlike the acknowledgement undo's
    hard-delete. It is still this slot's most recent departure, so it stays the row a
    duplicate runout trigger resolves onto and answers idempotently, and it is the row the
    calling trigger lane is holding as its return value.

    Nothing is re-bound. The slot is empty — the roll ran out and the bay cleared minutes
    ago — so a bind would claim a location for filament that is not there (doctrine rule 9),
    and it would additionally hand the FORWARD sweep the live assignment it requires, which
    is the one thing keeping these two directions from oscillating over a single row set
    (incident shape 26, the spool-211 ping-pong).

    Every exit before question 3 is SILENT on purpose: those describe every ordinary runout
    on the fleet, and an INFO line per runout saying "this was a normal runout" is the kind
    of noise the 2026-08-10 wave demoted six surfaces to remove. From the point a candidate
    exists, BOTH outcomes log — the stand-down is a decision this lane made and C4 is the
    arm that matters most.
    """
    delivered = spent.delivered_g
    assumed_g = float(spent.label_weight or 0.0)
    boundary = spent.created_at
    if (
        delivered is None  # not stamped spent: nothing has been settled yet
        or delivered <= 0  # fed nothing: no grams, so no evidence either way
        or assumed_g <= 0  # unpriceable row: no assumption to have got wrong
        or boundary is None  # no creation instant, so no provable "everything since" set
        or spent.data_origin != DATA_ORIGIN  # not a row the farm minted
        or not is_tagless_spool(spent)  # rule 10
        or (assumed_g - delivered) <= spool_respool._SPENT_DELIVERY_GAP_NOTE_G
    ):
        return None

    rows = (await db.execute(last_released_from_slot_stmt(printer_id, ams_id, tray_id).limit(2))).scalars().all()
    if not rows or rows[0].id != spent.id or bound_elsewhere(rows[0]):
        logger.info(
            "[RESPOOL] EARLY-RUNOUT NOT RE-ATTRIBUTED: spool %d is not this slot's most recent UNBOUND "
            "departure (printer %d AMS%d-T%d) — it is still bound, it has been re-bound elsewhere, or "
            "another roll has left the slot since. Assumption-tier evidence displaces nothing a live "
            "binding holds (invariant 11).",
            spent.id,
            printer_id,
            ams_id,
            tray_id,
        )
        return None

    departed = rows[1] if len(rows) > 1 else None
    remainder = 0.0
    if departed is None:
        why = "this slot has no earlier departure on record, so there is no row it could have displaced"
    elif bound_elsewhere(departed):
        why = (
            f"the departed row (spool {departed.id}) holds a live binding in another tray — a breadcrumb "
            "may not re-charge a roll the wire says is seated elsewhere (invariant 11)"
        )
    elif not is_tagless_spool(departed):
        why = f"the departed row (spool {departed.id}) is tag-identified — its identity is factual (rule 10)"
    elif departed.archived_at is not None:
        why = f"the departed row (spool {departed.id}) is archived — retired inventory is never resurrected"
    elif departed.spent_at is not None:
        why = f"the departed row (spool {departed.id}) ran out itself, so it had no remainder to hand over"
    elif float(departed.label_weight or 0.0) <= 0:
        why = f"the departed row (spool {departed.id}) has no label to price a remainder against"
    else:
        remainder = departed.remaining_g
        why = ""
        if abs(delivered - remainder) > _REATTRIBUTION_FIT_MARGIN_G:
            why = (
                f"it delivered {delivered:.0f} g against spool {departed.id}'s {remainder:.0f} g remainder, "
                f"{abs(delivered - remainder):.0f} g outside the {_REATTRIBUTION_FIT_MARGIN_G:.0f} g fit "
                "margin — the ambiguous band belongs to 'a different part-used roll' (operator ruling 4)"
            )

    if why:
        logger.info(
            "[RESPOOL] EARLY-RUNOUT NOT RE-ATTRIBUTED: spool %d ran out at %.0f g on an assumed %.0f g label "
            "(printer %d AMS%d-T%d) — %s. Its grams stand as delivered.",
            spent.id,
            delivered,
            assumed_g,
            printer_id,
            ams_id,
            tray_id,
            why,
        )
        return None

    runout_at = spent.spent_at
    moved = await _hand_charges_back(db, row=spent, predecessor=departed, boundary=boundary)
    if moved <= 0:
        # The fit was arithmetic; the MOVE needs per-print provenance. ``spool_usage_history``
        # is tracker-written only, so a manual ``weight_used`` edit or a scale weigh-in never
        # appears in it — and it stays on the row the operator put it on, exactly as the
        # forward direction refuses an attribution it cannot make exactly.
        logger.info(
            "[RESPOOL] EARLY-RUNOUT NOT RE-ATTRIBUTED: spool %d fits spool %d's remainder but carries no "
            "per-print charges to move (printer %d AMS%d-T%d) — no exact attribution is available.",
            spent.id,
            departed.id,
            printer_id,
            ams_id,
            tray_id,
        )
        return None

    departed.spent_at = runout_at
    spent.archived_at = datetime.utcnow()
    await db.commit()

    logger.info(
        "[RESPOOL] EARLY-RUNOUT RE-ATTRIBUTED: mistaken mint spool %d ran out at %.0f g on an assumed %.0f g "
        "label (printer %d AMS%d-T%d) — that is the %.0f g remainder spool %d still had when it left this "
        "slot, so %.0f g went back to it. Spool %d now reads %.0f g used and carries the runout; the mint is "
        "archived. The roll never changed, the mint did (rule 7's amended window, scenario C3).",
        spent.id,
        delivered,
        assumed_g,
        printer_id,
        ams_id,
        tray_id,
        remainder,
        departed.id,
        moved,
        departed.id,
        float(departed.weight_used or 0.0),
    )
    return departed


# --- W5 tagless fresh-roll prompt ------------------------------------------


def _live_tray(printer_id: int, ams_id: int, tray_id: int) -> dict | None:
    """The live tray dict for a slot from the printer's merged AMS state, or None.

    Regular AMS units only (tagless fresh-roll prompts key on AMS trays). Never
    raises — an unreachable printer reads as no tray.
    """
    try:
        state = printer_manager.get_status(printer_id)
    except Exception:  # noqa: BLE001 — resolution must never raise into the callback/route
        return None
    if state is None or not getattr(state, "raw_data", None):
        return None
    ams = state.raw_data.get("ams")
    if isinstance(ams, dict):
        ams = ams.get("ams", [])
    for unit in ams or []:
        if not isinstance(unit, dict):
            continue
        try:
            if int(unit.get("id", -1)) != ams_id:
                continue
        except (TypeError, ValueError):
            continue
        for tray in unit.get("tray", []) or []:
            if not isinstance(tray, dict):
                continue
            try:
                if int(tray.get("id", -1)) == tray_id:
                    return tray
            except (TypeError, ValueError):
                continue
    return None


def _tagless_fresh_payload(printer_id: int, ams_id: int, tray_id: int, spool: Spool) -> dict:
    """Frozen ``tagless_fresh_prompt`` WS payload (W5) — one origin for the live
    broadcast and the reconnect replay. Matches the frontend useWebSocket bridge +
    TaglessFreshPromptMessage: {printer_id, ams_id, tray_id, spool_id, remaining_g,
    material, rgba}."""
    return {
        "type": "tagless_fresh_prompt",
        "printer_id": printer_id,
        "ams_id": ams_id,
        "tray_id": tray_id,
        "spool_id": spool.id,
        "remaining_g": float((spool.label_weight or 0) - (spool.weight_used or 0)),
        "material": spool.material or "",
        "rgba": spool.rgba,
    }


async def _broadcast_tagless_fresh_prompt(printer_id: int, ams_id: int, tray_id: int, spool: Spool) -> None:
    await ws_manager.broadcast(_tagless_fresh_payload(printer_id, ams_id, tray_id, spool))


async def broadcast_tagless_fresh_dismissed(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Cross-client clear of a tagless fresh-roll prompt (either answer, W5)."""
    await ws_manager.broadcast(
        {
            "type": "tagless_fresh_prompt_dismissed",
            "printer_id": printer_id,
            "ams_id": ams_id,
            "tray_id": tray_id,
        }
    )


async def clear_fresh_prompt(db: AsyncSession, spool: Spool) -> None:
    """Answer a spool's fresh-roll prompt — NULL the durable pending stamp.

    Both answers land here ("Fresh roll" via :func:`apply_fresh_roll`, "Same spool"
    via the new-roll / fresh-roll-dismiss routes). Idempotent: an already-clear row is a no-op, so a
    double answer or a replayed toast costs nothing.
    """
    if spool.fresh_prompt_pending_at is None:
        return
    spool.fresh_prompt_pending_at = None
    await db.commit()


async def _maybe_prompt_fresh_roll(db: AsyncSession, printer_id: int, ams_id: int, tray_id: int) -> None:
    """W5 over-consumption / fresh-roll prompt for a physical cycle on a tagless slot.

    Reads the slot's kept assignment. A SPENT bound row **of any tag-ness** leaves the
    pending cycle for the W1 spent→mint transition (certain fresh roll — silent, no
    prompt). An UNBOUND slot leaves it too, for the pipeline to settle per outcome (see
    the branch comment — 2026-08-19, shape 32 layer 2). A NON-spent BOUND row whose tray
    is STILL PRESENT and is consumed past :data:`_FRESH_ROLL_PROMPT_USED_FRAC` of its
    label broadcasts ``tagless_fresh_prompt`` and stamps ``fresh_prompt_pending_at``.
    Every non-spent BOUND outcome (prompt, absent tray, sub-threshold, or a tagged row
    with nothing tagless to ask about) POPs the pending cycle — no latch is involved for
    non-spent rows.

    The spent check running FIRST is load-bearing (see the
    :data:`_pending_physical_cycles` survival invariant), not cosmetic ordering.

    This function runs only on a qualified-cycle edge, so the stamp is RE-stamped
    rather than deduped against: each new roll swap re-asks, which is the per-cycle
    contract the old in-memory set promised and never delivered (a stuck entry
    suppressed the slot's prompts for the rest of the process).
    """
    key = (printer_id, ams_id, tray_id)
    res = await db.execute(
        select(SpoolAssignment)
        .options(selectinload(SpoolAssignment.spool))
        .where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    assignment = res.scalar_one_or_none()
    spool = assignment.spool if assignment is not None else None
    # SPENT FIRST, for ANY tag-ness — order is load-bearing, not style. Spent-ness is
    # decided by the RUNOUT, never by whether the row carries an RFID identity, and this
    # function runs inside the very await that ARMS the pending cycle
    # (``note_physical_cycle``). With the tag-ness veto first, a spent TAGGED binding
    # discarded its own release signal here before either consumer (the pipeline's
    # REPLACE_SPENT arm, ``maybe_autoconfigure_bare_tray``'s spent gate) could ever see a
    # cycle — the same shape as the origin-veto bug one lane over, and the second lock on
    # the deadlocked slot (2026-08-07 #2, spool 226 / 001-H2S slot 1: a spent tagged row
    # latching its slot against the fresh roll physically seated in it).
    if spool is not None and spool.spent_at is not None:
        return  # leave the pending cycle for the W1 spent→mint transition (silent)
    if spool is None:
        # UNBOUND — and this lane may not decide what happens to the cycle, because it
        # runs BEFORE the outcome exists. It used to discard here, which is exactly
        # backwards for the shape that hits it most: a refill inside the ~3-minute
        # bay-clear→HMS gap arrives at an unbound slot (the runout's release landed
        # minutes before its own evidence), the pipeline de-bounces onto the exhausted
        # row, the runout then stamps it spent — and row 4a needs THIS cycle to fire
        # ``REPLACE_SPENT`` and hand the fresh roll its own row. Discarding it parked the
        # slot until a human pulled and re-seated (scenarios T8b/T8c, shape 32's layer 2).
        # ``slot_pipeline._settle_physical_cycle`` owns the decision now, per outcome, in
        # the apply step of the same pass — with no binding here there is nothing to
        # prompt about either way.
        return
    if not is_tagless_spool(spool):
        _pending_physical_cycles.discard(key)  # nothing tagless bound to latch/prompt
        return
    # Presence re-check before the stamp (2026-08-07). The cycle that got us here is a
    # presence GAIN, but the prompt asks "did you put a FRESH ROLL in this slot?" — a
    # question with no answer when the slot is EMPTY right now. A phantom edge (an AMS
    # engage transient, an identify flap) or an operator who pulled the roll straight
    # back out both land here, and the durable ``fresh_prompt_pending_at`` stamp made
    # that a toast the operator could not honestly answer. Re-read the LIVE merged tray
    # through the module's own accessors — the tray a callback was handed is by then
    # seconds stale, and the difference is exactly the case being filtered. Discard the
    # cycle: an absent slot has nothing to prompt about and nothing to latch.
    live = _live_tray(printer_id, ams_id, tray_id)
    if live is None or not tray_present(live):
        logger.debug(
            "tagless_fresh_prompt skipped: printer=%d AMS%d-T%d — tray is not present at prompt time",
            printer_id,
            ams_id,
            tray_id,
        )
        _pending_physical_cycles.discard(key)
        return
    label = spool.label_weight or 0
    used = spool.weight_used or 0
    if label > 0 and used >= _FRESH_ROLL_PROMPT_USED_FRAC * label:
        spool.fresh_prompt_pending_at = datetime.utcnow()
        await db.commit()
        await _broadcast_tagless_fresh_prompt(printer_id, ams_id, tray_id, spool)
        logger.info(
            "tagless_fresh_prompt broadcast: printer=%d AMS%d-T%d spool=%d used=%.0f/%d g",
            printer_id,
            ams_id,
            tray_id,
            spool.id,
            float(used),
            int(label),
        )
    _pending_physical_cycles.discard(key)  # non-spent processed (prompt or sub-threshold)


async def note_physical_cycle(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Record a QUALIFIED physical roll swap on a slot — the W1 latch release + W5 prompt.

    Called (guarded, awaited) from ``ams_presence.on_tray_observations`` on a genuine presence
    GAIN whose preceding absence lasted ≥ ``ams_presence._MIN_PHYSICAL_ABSENT_S``. Arms
    :data:`_pending_physical_cycles` (the spent-binding latch's release signal that the
    pipeline's REPLACE_SPENT arm / :func:`maybe_autoconfigure_bare_tray` consume on the
    next push) then
    runs the W5 over-consumption prompt in its OWN session (mirrors
    ``ams_presence.on_printer_terminal``). Never raises — a farm-side failure must never
    break the AMS callback chain.
    """
    key = (printer_id, ams_id, tray_id)
    _pending_physical_cycles.add(key)
    # Somebody physically moved a roll here, so a refused/unreflected write epoch is
    # about a slot that no longer exists as it was — the ONE edge that re-arms the
    # bare-tray lane on a still-occupied slot (its twin, the slot going empty, re-arms
    # through :func:`clear_autoconfig_dedup`). Before the prompt await: the cycle and
    # the re-arm are the same event, and nothing may observe one without the other.
    _rearm_autoconfig_epoch(printer_id, ams_id, tray_id, cause="a qualified physical cycle")
    try:
        from backend.app.core.database import async_session

        async with async_session() as db:
            await _maybe_prompt_fresh_roll(db, printer_id, ams_id, tray_id)
    except Exception:  # noqa: BLE001 — must never break the AMS callback chain
        logger.exception("note_physical_cycle W5 prompt failed for printer %d AMS%d-T%d", printer_id, ams_id, tray_id)


def qualified_cycle_pending(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Is a QUALIFIED physical roll swap pending on this slot? PEEK — never consumes.

    Read-only view of :data:`_pending_physical_cycles` for the slot pipeline, which
    needs the fact as a decision INPUT (``ResolutionContext.qualified_cycle_pending``)
    long before it knows whether the decision will spend it. Consumption is a separate,
    explicit act (:func:`consume_qualified_cycle`) so a pass that decides to KEEP the
    spent latch cannot silently swallow the swap evidence.
    """
    return (printer_id, ams_id, tray_id) in _pending_physical_cycles


def consume_qualified_cycle(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Spend this slot's pending physical cycle. True when there was one to spend.

    The W1 spent-latch RELEASE, popped exactly once — the same discard the branch-(3) /
    bare-tray transitions perform, exposed for the slot pipeline.

    ONE pop, two intents, and the difference lives in the caller's log line rather than in
    a second function: the pipeline's ``REPLACE_SPENT`` arm spends it as the EVIDENCE it
    acted on, while ``slot_pipeline._settle_physical_cycle`` retires it as the slot's
    lifecycle owner when a MINT or a BIND leaves a different row holding the slot (the
    bound that keeps a surviving cycle from replaying as a phantom swap two pushes later).
    """
    key = (printer_id, ams_id, tray_id)
    if key not in _pending_physical_cycles:
        return False
    _pending_physical_cycles.discard(key)
    _debounce_preserved_cycles.discard(key)  # the cycle is gone; its owner-mark means nothing
    return True


def mark_cycle_preserved_by_debounce(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Record that this slot's still-pending cycle survives only because a DE-BOUNCE kept it.

    Called from ``slot_pipeline._settle_physical_cycle``'s RECLAIM arm — the one outcome that
    preserves a cycle deliberately rather than by simply not touching it. Marking it is what
    gives that preservation an OWNER (:func:`expire_debounce_preserved_cycles`); see
    :data:`_debounce_preserved_cycles` for why an unowned one is a real hazard.

    A no-op (and False) when no cycle is actually pending: a mark without currency would
    outlive the thing it describes and expire nothing.
    """
    key = (printer_id, ams_id, tray_id)
    if key not in _pending_physical_cycles:
        return False
    _debounce_preserved_cycles.add(key)
    return True


async def expire_debounce_preserved_cycles(printer_id: int) -> int:
    """A print ended on this printer — retire its de-bounce-preserved cycles. Returns the count.

    The CAUSE bound on the T8b preservation (:data:`_debounce_preserved_cycles`). A cycle kept
    across a de-bounce is waiting for ONE specific thing: the runout of the print that was
    running when the bay emptied, whose ``spent_at`` stamp lands ~3 minutes later and fires
    ``REPLACE_SPENT`` on the next push. Once that print has reached a terminal, the wait is
    over — a stamp arriving afterwards belongs to a later print and a later physical story,
    and honouring it would swap a roll on evidence of an event that never happened.

    **The spent stamp WINS a race with this hook**, deliberately: a slot whose bound row is
    already stamped has its evidence in hand, the swap is due on the very next push, and the
    terminal callback and that push are not ordered against each other. So a stamped row keeps
    its cycle and T8b's self-heal survives a print that ends inside the gap; only the
    UNSTAMPED ones — the ones still waiting for evidence that is no longer coming — are
    retired. That is the same asymmetry rule 8 states for the stamp itself: a missed swap
    self-heals forward at the next physical cycle, an unfounded one is a mint the operator
    never asked for.

    Its own session (the completion callback has none to lend), and never raises — this is a
    lifecycle hook on the terminal path (invariant 10).
    """
    keys = [key for key in _debounce_preserved_cycles if key[0] == printer_id]
    if not keys:
        return 0
    expired = 0
    try:
        from backend.app.core.database import async_session

        async with async_session() as db:
            for key in keys:
                _, ams_id, tray_id = key
                spent_at = await db.scalar(
                    select(Spool.spent_at)
                    .join(SpoolAssignment, SpoolAssignment.spool_id == Spool.id)
                    .where(
                        SpoolAssignment.printer_id == printer_id,
                        SpoolAssignment.ams_id == ams_id,
                        SpoolAssignment.tray_id == tray_id,
                    )
                )
                if spent_at is not None:
                    continue  # the evidence landed — the swap is due, not expired
                _debounce_preserved_cycles.discard(key)
                _pending_physical_cycles.discard(key)
                expired += 1
                logger.info(
                    "[TAGLESS] printer %d AMS%d-T%d: de-bounce-preserved physical cycle retired at the job "
                    "terminal — the runout it was waiting for never arrived, so a later spent stamp must not "
                    "replay it as a roll swap",
                    printer_id,
                    ams_id,
                    tray_id,
                )
    except Exception:  # noqa: BLE001 — a terminal lifecycle hook must never crash the callback
        logger.exception("expire_debounce_preserved_cycles failed for printer %s", printer_id)
    return expired


async def apply_fresh_roll(
    db: AsyncSession,
    spool: Spool,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    *,
    brand: str | None = None,
    label_weight: int | None = None,
    cost_per_kg: float | None = None,
    note: str | None = None,
) -> Spool:
    """Answer a W5 fresh-roll prompt with "Fresh roll" — archive the current tagless
    row and mint+bind+push a replacement (default-vs-tray via the shared transition),
    applying the operator's optional brand/label_weight/cost_per_kg/note to the new row.
    Clears the departed row's pending stamp. Returns the new spool. Raises ``ValueError``
    when the slot's live tray can't be resolved (the route maps it to HTTP 409)."""
    tray = _live_tray(printer_id, ams_id, tray_id)
    if tray is None:
        raise ValueError("slot is no longer readable")
    new_spool = await _replace_row_after_cycle(
        db,
        printer_id,
        ams_id,
        tray_id,
        tray,
        spool,
        new_fields={"brand": brand, "label_weight": label_weight, "cost_per_kg": cost_per_kg, "note": note},
    )
    await clear_fresh_prompt(db, spool)
    _pending_physical_cycles.discard((printer_id, ams_id, tray_id))
    return new_spool


async def pending_fresh_prompts(db: AsyncSession) -> list[dict]:
    """Every still-open tagless fresh-roll prompt, as ready-to-send WS payloads (W5).

    The ONE snapshot of the durable ``fresh_prompt_pending_at`` stamp, consumed by both
    the reconnect replay (:func:`rebroadcast_unresolved_tagless_prompts`) and the REST
    fallback (``GET /inventory/prompts/pending``) so the two can never disagree about
    what is outstanding.

    A stamp alone is not proof the prompt still applies: it is re-validated against
    durable state (the row must still be bound, tagless, non-spent, non-archived) and
    LIVE state (the slot must still physically hold a spool). Stale rows are skipped,
    never mutated — the stamp is cleared only by an operator answer, and a slot that is
    merely offline right now must still prompt when it comes back.
    """
    res = await db.execute(
        select(SpoolAssignment)
        .options(selectinload(SpoolAssignment.spool))
        .join(Spool, SpoolAssignment.spool_id == Spool.id)
        .where(
            Spool.fresh_prompt_pending_at.is_not(None),
            Spool.archived_at.is_(None),
            Spool.spent_at.is_(None),
        )
    )
    payloads: list[dict] = []
    for assignment in res.scalars().all():
        spool = assignment.spool
        if spool is None or not is_tagless_spool(spool):
            continue
        tray = _live_tray(assignment.printer_id, assignment.ams_id, assignment.tray_id)
        if tray is None or not tray_present(tray):
            continue
        payloads.append(_tagless_fresh_payload(assignment.printer_id, assignment.ams_id, assignment.tray_id, spool))
    return payloads


async def rebroadcast_unresolved_tagless_prompts(db: AsyncSession, send) -> int:
    """Replay unresolved ``tagless_fresh_prompt`` events to a (re)connecting client (W5).

    Sibling of ``spool_respool.rebroadcast_unresolved_respool_prompts``. The prompt WS
    event is fire-once (``ws_manager.broadcast`` keeps no backlog and no-ops entirely
    when nobody is connected — the 19:39 prompt that reached zero clients), so this
    replays the durable snapshot :func:`pending_fresh_prompts` builds. Returns the count
    re-sent. Never raises (a reconnect must not break on a farm-side hook).
    """
    sent = 0
    try:
        payloads = await pending_fresh_prompts(db)
    except Exception:  # noqa: BLE001 — a reconnect must never break on the replay hook
        logger.exception("tagless_fresh_prompt re-broadcast snapshot failed")
        return 0
    for payload in payloads:
        try:
            await send(payload)
            sent += 1
        except Exception:  # noqa: BLE001 — one slot's failure must not abort the replay
            logger.exception(
                "tagless_fresh_prompt re-broadcast failed for printer %s AMS%s-T%s",
                payload.get("printer_id"),
                payload.get("ams_id"),
                payload.get("tray_id"),
            )
    if sent:
        logger.info("Re-broadcast %d unresolved tagless_fresh_prompt(s) to a (re)connecting client", sent)
    return sent


# --- settle / busy gates (read by the pipeline as decision inputs) ---------


def _mint_settling(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """True while a just-inserted spool's gain is still settling (F1).

    The insert's first MQTT push carries the slot's CONFIG but not yet its tag —
    the firmware's own RFID read lands ~1 s later. Minting on that push creates a
    provisional tagless row the tag read immediately hard-deletes. A gain younger
    than :data:`_MINT_SETTLE_S` therefore defers a FRESH mint by one push; a slot
    with no recorded gain (restart, never observed) reads as settled, so the defer
    can never wedge a slot.
    """
    age = ams_presence.recent_gain_age(printer_id, ams_id, tray_id)
    return age is not None and age < _MINT_SETTLE_S


def slot_is_settling(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Is EITHER settle window open on this slot? Read-only, changes nothing.

    The two windows guard different things and the slot pipeline needs both as one
    decision input (``ResolutionContext.settling``): :func:`_mint_settling` (F1) stops a
    fresh row being minted inside the firmware's own post-insert RFID read, and
    :func:`_config_settling` stops an identity being PUBLISHED into a slot whose
    identity is still an open question. Either one open means "not this push".
    """
    return _mint_settling(printer_id, ams_id, tray_id) or _config_settling(printer_id, ams_id, tray_id)


def _printer_busy(printer_id: int, *, on_error: bool = True) -> bool:
    """True while the printer is mid-job (RUNNING/PAUSE).

    Delegates to ``ams_presence._printer_running`` rather than re-deriving the
    predicate: that module already owns the running-state reading every AMS-side
    guard shares, and a second copy is exactly the drift this fork avoids. Never
    raises — ``on_error`` is what an unreadable state resolves to, and the two callers
    need OPPOSITE safe directions, so each states its own: the idle-only identity
    reconcile takes the default True (unknown ⇒ assume busy ⇒ do not write), while
    :func:`_config_settling` passes False (unknown ⇒ assume idle ⇒ keep the settle gate
    ON). Both resolve to "protect the wire"; only the polarity differs.
    """
    try:
        return ams_presence._printer_running(printer_manager.get_status(printer_id))
    except Exception:  # noqa: BLE001 — must never break the AMS callback chain
        return on_error


# --- D3b: bare-tray write-epoch accounting ---------------------------------


def _autoconfig_latched(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Has this slot's write epoch ENDED, i.e. is the lane done writing to it?"""
    epoch = _autoconfig_epochs.get((printer_id, ams_id, tray_id))
    return epoch is not None and epoch.ended is not None


def _end_autoconfig_epoch(
    printer_id: int,
    ams_id: int,
    tray_id: int,
    epoch: _AutoconfigEpoch,
    *,
    verdict: str,
    detail: str,
) -> None:
    """Latch a slot's write epoch closed: one INFO line, no more writes, ask the wire.

    The single epoch-end for all three failure shapes — a ``fail`` ack, a ``success``
    ack the tray never reflects, and no ack at all — because they are the same
    conclusion: this slot is not taking the config, and repeating the write is not what
    changes that. Only ever called on an OPEN epoch (:func:`_autoconfig_latched` gates
    every caller), so the line and the report request happen exactly once per epoch.

    The pushall is the honest follow-up to stopping: what the farm believes about the
    slot is now a hypothesis the wire has contradicted, and a full report is the one
    thing that can replace it. It self-paces and answers False when the printer is
    disconnected or the floor has not elapsed — a deferral is fine, the latch does not
    depend on it (invariant 2: callers may defer, never pre-approve).
    """
    _autoconfig_epochs[(printer_id, ams_id, tray_id)] = epoch._replace(ended=verdict)
    logger.info(
        "Bare-tray auto-config STOPPED for printer %d AMS%d-T%d: %s (%d attempt(s), %d success ack(s)) "
        "— no further config writes until a presence/identity edge re-arms the slot",
        printer_id,
        ams_id,
        tray_id,
        detail,
        epoch.publishes,
        epoch.acked_ok,
    )
    printer_manager.request_evidence_pushall(printer_id, verdict)


def _rearm_autoconfig_epoch(printer_id: int, ams_id: int, tray_id: int, *, cause: str) -> None:
    """Drop a slot's write epoch — the ONLY way a latched slot ever writes again.

    Called on the three EDGES that make the situation genuinely new rather than the
    same refused hypothesis restated: a qualified physical roll cycle
    (:func:`note_physical_cycle`), the slot reading empty
    (:func:`clear_autoconfig_dedup`), and the firmware reflecting a config
    (:func:`note_config_adopted`). Deliberately NOT a timer: a latch that expires on
    its own is the unbounded retry loop again, only slower.
    """
    epoch = _autoconfig_epochs.pop((printer_id, ams_id, tray_id), None)
    if epoch is not None and epoch.ended is not None:
        logger.info(
            "Bare-tray auto-config re-armed for printer %d AMS%d-T%d on %s (epoch had ended: %s)",
            printer_id,
            ams_id,
            tray_id,
            cause,
            epoch.ended,
        )


def _note_autoconfig_attempt(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Count one committed config write against the slot's epoch, opening it if new.

    Counted at the COMMIT point — every gate passed, immediately before the publish —
    for two reasons. The retry window is already stamped by then, so the attempt has
    spent its slot in the cadence whether or not the transport succeeded (a write that
    never reached the broker is an attempt that did not land, which is exactly what the
    ladder counts). And the ack cannot arrive before the publish, so an epoch always
    exists by the time the wire answers it.
    """
    key = (printer_id, ams_id, tray_id)
    epoch = _autoconfig_epochs.get(key, _AutoconfigEpoch())
    _autoconfig_epochs[key] = epoch._replace(publishes=epoch.publishes + 1)


def note_config_adopted(printer_id: int, ams_id: int, tray_id: int) -> None:
    """The firmware is reporting a configuration for this slot — the epoch SUCCEEDED.

    Adoption is the lane's only positive success signal: an ack says the write was
    accepted, a non-empty ``tray_type`` says it was applied, and the incident this
    accounting closes is precisely the gap between the two. Called from the two places
    that see a slot's live tray dict — :func:`maybe_autoconfigure_bare_tray`'s
    already-configured guard and :func:`reconcile_slot_config`'s walk — so an adopted
    slot cannot carry a stale strike count into a LATER bare episode (a firmware slot
    reset makes a configured slot bare again with no presence edge anywhere).

    A slot with no open epoch is the ordinary case (every RFID tray reports a type):
    the drop is then a no-op.
    """
    _rearm_autoconfig_epoch(printer_id, ams_id, tray_id, cause="config adopted")


def on_ams_command_result(printer_id: int, echo: dict) -> None:
    """Consume the firmware's ACK of an AMS write — the wire's verdict on our config.

    Entry hook for ``BambuMQTTClient.on_ams_command_result``, wired with the printer id
    in ``printer_manager.connect_printer``. Runs SYNCHRONOUSLY on the MQTT thread: pure
    process-memory bookkeeping plus (at most) one paced pushall request, no DB and no
    awaits, so the bare-tray lane cannot publish again between a refusal arriving and
    being recorded. Never raises — a farm-side failure must never break the MQTT
    callback chain.

    Only ``ams_filament_setting`` is judged here. ``extrusion_cali_sel`` echoes on the
    same lane (routing both is the client's contract) but a K selection is not the
    filament config: it addresses the tray in the GLOBAL id space, and a refused
    calibration says nothing about whether the identity write landed.

    Only an OPEN epoch is acted on. An ack for a write this lane never made — an
    operator assign, the pipeline's pre-config one-shot — has nothing to stop, and a
    latch minted from it would suppress attempts that were never attempted.
    """
    try:
        if echo.get("command") != "ams_filament_setting":
            return
        result = str(echo.get("result") or "").strip().lower()
        if result not in ("fail", "success"):
            return
        ams_id = parse_int_field(echo.get("ams_id"))
        tray_id = parse_int_field(echo.get("tray_id"))
        if ams_id is None or tray_id is None or ams_id in _ECHO_VIRTUAL_AMS_IDS:
            return

        key = (printer_id, ams_id, tray_id)
        epoch = _autoconfig_epochs.get(key)
        if epoch is None or epoch.ended is not None:
            return

        if result == "success":
            # NOT an epoch end: the firmware accepting a write is not the firmware
            # applying it, and the whole incident class lives in that gap. Counted only
            # so the closing line can name which of the two silences was observed.
            _autoconfig_epochs[key] = epoch._replace(acked_ok=epoch.acked_ok + 1)
            return

        reason = echo.get("reason")
        detail = 'the firmware answered result="fail"'
        if reason not in (None, ""):
            detail = f"{detail} (reason={reason})"
        _end_autoconfig_epoch(printer_id, ams_id, tray_id, epoch, verdict="config_refused", detail=detail)
    except Exception:  # noqa: BLE001 — must never break the MQTT callback chain
        logger.exception("AMS command-result handling failed for printer %s: %s", printer_id, echo)


# --- D3b: bare-tray auto-config --------------------------------------------


def _own_tagless_slot(tray: dict, assignment: SpoolAssignment | None) -> bool:
    """Is this slot a seated, non-RFID tray bound to a LIVE row THIS module minted?

    The shared eligibility both wire-config arms rest on — :func:`maybe_autoconfigure_bare_tray`
    (bare tray, nothing configured) and :func:`maybe_harmonize_backup_identity`
    (configured tray whose identity drifted from the default). Neither arm may write to
    a slot that is not ours:

    * ``tray_present`` — a write to an empty slot configures nothing and a write to an
      unknown one is a guess;
    * no valid RFID tag — a config write racing a firmware tag read is the 2026-07-18
      HMS ``0700_0081`` class;
    * a binding, holding a spool, whose ``data_origin`` is this module's own
      :data:`DATA_ORIGIN` — an operator- or RFID-bound row is somebody's STATEMENT
      about that slot and is never overwritten;
    * not spent — a spent row is the durable "ran dry" latch, released only by a
      qualified physical cycle (W1). The bare arm reaches that transition ABOVE this
      predicate, which is why it can call it afterwards without losing the branch.

    Deliberately NOT here: everything that needs a printer id or the DB (the
    identify/drying defers, the write-epoch latch, the settle gates, the retry cadence,
    the tagless-default setting). Those are per-arm OCCASION gates, not "is this slot
    ours", and folding them in would hide a refusal behind an eligibility answer.
    """
    if not tray_present(tray):
        return False
    if is_valid_tag(tray.get("tag_uid", "") or "", tray.get("tray_uuid", "") or ""):
        return False
    if assignment is None or assignment.spool is None:
        return False
    if assignment.spool.data_origin != DATA_ORIGIN:
        return False
    return assignment.spool.spent_at is None


async def maybe_autoconfigure_bare_tray(
    db: AsyncSession, printer_id: int, ams_id: int, tray_id: int, tray: dict, *, force: bool = False
) -> bool:
    """Push a default filament to a BARE tray (spool present, nothing configured).

    Makes an unconfigured third-party spool usable — including mid-print, where
    the newly-configured slot joins the firmware backup pool. Returns True when a
    config push was attempted this tick.

    Trigger: tray PRESENT (state 10/11) AND tray_type empty AND no valid tag AND
    a non-empty ``tagless_default_filament`` setting — clearing that setting is the
    ONE off switch (the 2026-08-10 wave retired the separate kill-switch setting;
    tagless gram tracking is load-bearing, not a preference).
    The config push self-heals through TWO service lanes (gated by
    :data:`_AUTOCONFIG_RETRY_S` either way) until the firmware reports a non-empty
    tray_type and the slot leaves this branch: ``main.on_ams_change``'s bare-tray
    branch while the AMS state keeps churning, and :func:`reconcile_slot_config` on
    the scheduler tick. The second lane is the guaranteed one — the callback is
    change-gated on ``bambu_mqtt``'s AMS state hash, so a settled AMS stops firing
    it entirely (2026-07-24 incident).

    ``force=True`` bypasses ONLY the :data:`_AUTOCONFIG_RETRY_S` window (every
    other guard — presence, tray_type-empty, RFID, settings, operator/RFID-bound
    slot — still applies). ``spool_recovery`` uses it for a one-shot bare-tray
    sweep when a mid-print jam has no configured replacement, so a present-but-bare
    backup spool can be enrolled without waiting out the retry cadence.

    Both self-healing lanes are BOUNDED by the slot's write epoch: a ``result:"fail"``
    ack (:func:`on_ams_command_result`) or :data:`_AUTOCONFIG_MAX_PUBLISHES` attempts the
    tray never reflects ends the epoch, and nothing writes to that slot again until a
    presence/identity edge re-arms it (:func:`_rearm_autoconfig_epoch`). Unbounded,
    "retry until the firmware reflects it" has no exit when the firmware's answer is no.
    """
    if not tray_present(tray):
        return False
    if (tray.get("tray_type") or "").strip():
        # ADOPTED: the firmware is reflecting a config for the slot, so any open write
        # epoch ended in success. Both callers pre-filter on bare-ness, so this guard is
        # not the reliable observer — ``reconcile_slot_config``'s walk carries the same
        # observation for the slots that stop reaching here at all.
        note_config_adopted(printer_id, ams_id, tray_id)
        return False  # already configured — not bare
    if is_valid_tag(tray.get("tag_uid", "") or "", tray.get("tray_uuid", "") or ""):
        return False  # RFID tray — not tagless
    default = await _tagless_default(db)
    if default is None:
        return False  # setting cleared → feature off

    # Defer a doomed config push while the AMS is mid-identify or drying: the write
    # would collide with the RFID read (HMS 0700_2x00_0001_0081) or disengage the
    # drying tray (HMS 0700_C069). Return BEFORE the retry window is stamped so it is
    # not burned on a push that never went out. force= bypasses only the retry
    # window — never these hardware-state guards.
    if ams_presence.identify_in_flight(printer_id, ams_id, tray_id) or ams_presence.unit_drying(printer_id, ams_id):
        logger.debug(
            "Deferring bare-tray auto-config for printer %d AMS%d-T%d: AMS identify/drying in progress",
            printer_id,
            ams_id,
            tray_id,
        )
        return False

    key = (printer_id, ams_id, tray_id)

    # LATCHED: this slot's write epoch ENDED (refused on the wire, or written three
    # times and never reflected) and nothing since has made the situation new. Refused
    # here — above the DB lookup, and above the spent-cycle transition below, which is
    # unreachable while latched anyway because the qualified cycle that unlocks it
    # re-arms the epoch in the same call. ``force=`` does not bypass this: it bypasses
    # only the retry window, and a forced sweep supplies a fresh OCCASION, never new
    # evidence about a slot the wire is refusing.
    if _autoconfig_latched(printer_id, ams_id, tray_id):
        return False

    res = await db.execute(
        select(SpoolAssignment)
        .options(selectinload(SpoolAssignment.spool))
        .where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    assignment = res.scalar_one_or_none()

    # W1: a SPENT binding is the "ran dry" latch — never re-push a spent row's config.
    # Only a QUALIFIED physical roll swap (a pending cycle recorded by
    # note_physical_cycle) releases it into the archive→unlink→default-mint→push
    # transition. Checked BEFORE stamping the retry window so a latched slot never burns
    # it, and ABOVE the data-origin veto below because spent-ness is decided by the
    # runout, not by who minted the row: an rfid_auto row that ran dry is a dead core
    # like any other, and vetoing it first left a spent TAGGED row latching its slot
    # against the fresh roll physically seated in it forever (printer 4 tray 2,
    # 2026-08-07 — spool 212, 1121.5 g on a 1000 g label, a ~90 %-full replacement
    # unread beneath it for a day). The veto still protects every LIVE non-ams_auto row,
    # which is what it was written for.
    if assignment is not None and assignment.spool is not None and assignment.spool.spent_at is not None:
        if key not in _pending_physical_cycles:
            return False  # latched — no re-push of a spent slot's config
        _pending_physical_cycles.discard(key)  # consume the cycle
        await _replace_row_after_cycle(db, printer_id, ams_id, tray_id, tray, assignment.spool)
        return True

    if assignment is not None and not _own_tagless_slot(tray, assignment):
        # Operator- or RFID-bound slot (or an orphan) — never overwrite. Only our
        # OWN auto-minted default is eligible for a self-healing re-push. Shared with
        # the harmonise arm (:func:`_own_tagless_slot`) so "ours" has one definition;
        # the spent half of that predicate is already decided above, where a qualified
        # cycle takes the replace transition instead of returning.
        return False

    # Settle before the FIRST mint on this slot (F1): a bare tray whose spool was
    # just inserted may still have the firmware's RFID read in flight, and a row
    # minted now is the one the tag read hard-deletes. A slot we ALREADY track only
    # re-pushes config, so it is not deferred. Before the retry window, so the defer
    # does not burn it.
    if assignment is None and _mint_settling(printer_id, ams_id, tray_id):
        logger.debug(
            "Deferring bare-tray mint for printer %d AMS%d-T%d: insertion still settling",
            printer_id,
            ams_id,
            tray_id,
        )
        return False

    # Never publish an identity into an UNRESOLVED slot (:func:`_config_settling`):
    # a spool inserted over a surviving tagless binding reads BARE for ~1 s while the
    # firmware runs its own RFID read, and a config write landing in that window
    # destroys the RFID-detected state permanently (2026-07-25). This applies to the
    # re-push arm as much as the mint arm — the 6-hour phantom binding came from a
    # re-push — so it sits below the assignment lookup and above the retry window,
    # mirroring the identify/drying defers: a deferred push must not burn the window.
    # _push_config carries the same gate as the funnel backstop; failing here first is
    # what keeps the cadence intact.
    if _config_settling(printer_id, ams_id, tray_id):
        logger.debug(
            "Deferring bare-tray auto-config for printer %d AMS%d-T%d: slot identity still settling",
            printer_id,
            ams_id,
            tray_id,
        )
        return False

    # The retry-cadence gate is the LAST guard: every ineligible/deferred path above
    # returns without stamping, so the window is armed only by an attempt that is
    # actually about to publish. force= clears the previous stamp instead of skipping
    # the gate, so a forced push still re-arms the cadence for the pushes after it.
    if force:
        _autoconfig_window.clear(key)
    if not _autoconfig_window.allow(key):
        return False  # config attempt still inside its retry window

    # STRIKE LADDER. The window has just opened for attempt N+1, which means every
    # earlier attempt has had a FULL settle window to be reflected — and the tray still
    # reports no configuration, or this branch would not be running. At
    # :data:`_AUTOCONFIG_MAX_PUBLISHES` that is the same conclusion an explicit refusal
    # states, reached the slow way (the "success ack, never applied" and "no ack at
    # all" shapes both land here), so it ends the epoch identically. Above the mint so
    # a latching pass never creates a spool row for a write it is not going to make.
    epoch = _autoconfig_epochs.get(key, _AutoconfigEpoch())
    if epoch.publishes >= _AUTOCONFIG_MAX_PUBLISHES:
        _end_autoconfig_epoch(
            printer_id,
            ams_id,
            tray_id,
            epoch,
            verdict="config_unadopted",
            detail="the config was written and the tray still reports none applied",
        )
        return False

    if assignment is None:
        spool = await mint_tagless_spool(db, default_filament=default)
        bound = await _assign_from_setting(db, spool, printer_id, ams_id, tray_id, default)
        await db.commit()
        if bound is None:
            # Writer refused the bind (move damper). Nothing owns the slot, so a config
            # push would publish an identity for a binding that does not exist.
            logger.warning(
                "Bare-tray mint %d was not bound to printer %d AMS%d-T%d (writer refused) — no config push",
                spool.id,
                printer_id,
                ams_id,
                tray_id,
            )
            return False
    else:
        # Our own default already tracked but the firmware hasn't applied it yet
        # (failed / slow push) — re-push, don't re-mint.
        spool = assignment.spool

    _note_autoconfig_attempt(printer_id, ams_id, tray_id)
    await _push_config(db, spool, printer_id, ams_id, tray_id, tray)
    return True


# --- backup-group identity harmonisation -----------------------------------


def _row_backup_group_key(spool: Spool) -> str | None:
    """The ledger row's own :func:`tray_fields.backup_group_key`, for the log line.

    The row is rendered through the SAME key the wire side is, so the two halves of a
    harmonise INFO line are directly comparable and the operator can see which of the
    key's two dimensions is split. Never a decision input — the decision is
    :func:`canonical_default_identity`.
    """
    return backup_group_key(
        {
            "tray_type": spool.material or "",
            "tray_info_idx": spool.slicer_filament or "",
            "tray_color": spool.rgba or "",
            "nozzle_temp_min": spool.nozzle_temp_min,
            "nozzle_temp_max": spool.nozzle_temp_max,
        }
    )


async def maybe_harmonize_backup_identity(
    db: AsyncSession,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tray: dict,
    assignment: SpoolAssignment | None,
) -> bool:
    """Re-align a CONFIGURED tagless slot onto the tagless default's EXACT identity.

    :func:`maybe_autoconfigure_bare_tray`'s sibling on the opposite predicate: that arm
    gives an unconfigured tray a usable identity, this one fixes a configured tray whose
    identity is the default's filament spelled differently — so the firmware will
    actually pair the slots into one auto-refill backup group.

    INCIDENT (010-H2S, 2026-08-21). Slots 1+2 carried tray colour ``161616FF`` — a Bambu
    Studio / touchscreen slot edit that accepted the PETG-HF preset's own default colour
    — while slots 3+4 carried the farm's ``000000FF``. Same material, same preset, same
    temps, ``colors_similar`` to each other, and the firmware pairs on an EXACT colour
    match, so the two pairs were separate groups: ``hms-events`` shows 69 auto-switches
    inside 1↔2, 14 inside 3→4, and none across. The printer ran dry on slot 2 twice in
    28 h with a full black roll one slot away and AMS Filament Backup ON. The farm had
    been harmonising the PRESET dimension since the 011-H2S ``GFG99`` fix, and the
    temperature range since W4 (which groups nothing — measured 2026-08-25); the colour
    dimension had no lane at all, and a slot the operator edited on the touchscreen
    could never come back.

    TWO ORDERED STEPS, both driven by the one predicate
    (:func:`canonical_default_identity`):

    * **ROW** — when the ledger row itself is off-canonical, write the default's four
      identity fields onto it. Rows minted from a tray inherit whatever the tray read,
      which is how ``161616FF`` got into the ledger in the first place; leaving the row
      wrong means every future re-push re-publishes the split.
    * **WIRE** — when the live tray still reads something other than the (now canonical)
      identity, publish it through :func:`_push_config`, the module's ONE wire write.

    Every guard the bare arm carries applies, for the same reasons and in the same order:
    ownership (:func:`_own_tagless_slot`), the tagless-default setting, the
    identify/drying hardware defers, the write-epoch latch, the settle gate, the retry
    cadence and the strike ladder. Two placements are deliberate. The ROW step runs ABOVE
    the wire guards because correcting the ledger is a DB write that risks nothing on the
    wire and must not be held hostage to a slot the firmware is refusing. The wire-delta
    test runs above the DEFER logging, so a settled, already-canonical slot stays silent
    every pass instead of narrating a deferral it does not need.

    Refuses outright when the row is not the default's filament
    (:func:`_eligible_for_default_identity`): a different colour, a different material or
    a different SPECIFIC preset is an operator statement, and harmonising it would push
    OUR guess over THEIR answer (doctrine rule 2). Returns True when a config push was
    attempted this pass.
    """
    if not (tray.get("tray_type") or "").strip():
        return False  # bare — maybe_autoconfigure_bare_tray owns that shape
    if not _own_tagless_slot(tray, assignment):
        return False
    spool = assignment.spool  # narrowed by _own_tagless_slot
    default = await _tagless_default(db)
    if default is None:
        return False  # setting cleared → feature off
    if not _eligible_for_default_identity(
        default, slicer_filament=spool.slicer_filament, material=spool.material, rgba=spool.rgba
    ):
        return False  # not the default's filament — not ours to canonicalise

    # (i) ROW — make the ledger the canonical identity before anything reads it again.
    canon = canonical_default_identity(
        default,
        slicer_filament=spool.slicer_filament,
        material=spool.material,
        rgba=spool.rgba,
        nozzle_temp_min=spool.nozzle_temp_min,
        nozzle_temp_max=spool.nozzle_temp_max,
    )
    if canon is not None:
        changes: list[str] = []
        for field in ("slicer_filament", "rgba", "nozzle_temp_min", "nozzle_temp_max"):
            new = canon[field]
            if new is None:
                continue  # the default does not carry this dimension — nothing to state
            old = getattr(spool, field)
            if old == new:
                continue
            setattr(spool, field, new)
            if field == "slicer_filament":
                spool.slicer_filament_name = None  # the builtin name belonged to the old id
            changes.append(f"{field} {old!r}→{new!r}")
        if changes:
            await db.commit()
            logger.info(
                "[tagless] harmonise printer=%d AMS%d-T%d spool=%d row: %s",
                printer_id,
                ams_id,
                tray_id,
                spool.id,
                ", ".join(changes),
            )

    # (ii) WIRE — the tray's own reading, judged by the SAME predicate the reconcile
    # walk's adoption test uses, so the two can never disagree about this slot.
    if _tray_canonical_delta(default, tray) is None:
        return False  # the firmware already reports the canonical identity

    if ams_presence.identify_in_flight(printer_id, ams_id, tray_id) or ams_presence.unit_drying(printer_id, ams_id):
        logger.debug(
            "Deferring backup-group harmonise for printer %d AMS%d-T%d: AMS identify/drying in progress",
            printer_id,
            ams_id,
            tray_id,
        )
        return False
    if _autoconfig_latched(printer_id, ams_id, tray_id):
        return False  # this slot's write epoch ended — the wire refused, or never reflected
    if _config_settling(printer_id, ams_id, tray_id):
        logger.debug(
            "Deferring backup-group harmonise for printer %d AMS%d-T%d: slot identity still settling",
            printer_id,
            ams_id,
            tray_id,
        )
        return False

    key = (printer_id, ams_id, tray_id)
    if not _autoconfig_window.allow(key):
        return False  # still inside the shared re-push cadence

    epoch = _autoconfig_epochs.get(key, _AutoconfigEpoch())
    if epoch.publishes >= _AUTOCONFIG_MAX_PUBLISHES:
        _end_autoconfig_epoch(
            printer_id,
            ams_id,
            tray_id,
            epoch,
            verdict="config_unadopted",
            detail="the canonical identity was written and the tray still reports its own",
        )
        return False

    logger.info(
        "[tagless] harmonise printer=%d AMS%d-T%d spool=%d wire: tray reads %s vs canonical %s — pushing",
        printer_id,
        ams_id,
        tray_id,
        spool.id,
        backup_group_key(tray),
        _row_backup_group_key(spool),
    )
    _note_autoconfig_attempt(printer_id, ams_id, tray_id)
    await _push_config(db, spool, printer_id, ams_id, tray_id, tray)
    return True


# --- durable slot-config reconcile (scheduler tick) ------------------------


async def _age_bound_presence_stale(
    db: AsyncSession,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tray: dict,
    assignment: SpoolAssignment | None,
    now: float,
    manager,
) -> None:
    """Age a BOUND slot whose merged presence has gone stale, and ask ONCE per episode.

    A binding claims WHERE a roll is (doctrine rule 9), so it is only as good as the
    presence signal that can contradict it. Two readings leave that claim unchecked, and
    BOTH are stale — the arm treats them identically:

    * **None (unknown)** — nothing in the merged view asserts anything, so every consumer
      fails OPEN. The A1/P1S constant ``state=3`` and the H2C long-idle ``state=0``
      dialects sit here permanently.
    * **False (asserted empty) under a LIVE binding** — the merged lane HAS the release
      evidence and the binding is still standing, which can only mean the deciding RAW
      lane never saw it. Measured on printer 1 (2026-08-09): four bound slots merged at
      state 9, two of them asserting ``tray_type: ""``, zero releases in two days,
      because H2S omits a stable-empty tray from its incrementals entirely — the cleared
      shape's only carrier is a full report, and nothing asked for one. Requesting that
      report IS the cure: its raw observations reach the pipeline, which releases.

    Exempt, because their presence is not a claim this can check: a PRE-CONFIGURED
    binding (a deliberate weigh-then-assign intent over an empty slot) and a SPENT one
    (the durable ran-dry latch) — the same two rule 9 exempts from release.

    **EPISODE SEMANTICS: ASK UNTIL ANSWERED.** An episode is one continuous run of the
    same reading on the same binding. It matures after :data:`_BOUND_PRESENCE_STALE_AFTER_S`
    and then ASKS on a backoff ladder — at maturity, then :data:`_PRESENCE_ASK_GAPS_S`
    later each time, then every :data:`_PRESENCE_ASK_INTERVAL_S` — until the reading moves.
    It closes only when the presence VALUE changes (including None↔False) or the binding
    does; never on a timer.

    One ask per episode was the previous rule, on the reasoning that a repeat answers
    nothing. That holds only if the first ask ARRIVED, and the ways it silently does not —
    a disconnected printer, the 120 s pacing floor already spent by the wire-side drain, a
    report that crossed the request — are precisely the states a slot is in when its
    presence has stopped resolving. From inside, "asked and answered" and "never asked"
    look identical; the ladder makes the farm act on the difference it cannot see.

    The OPERATOR is told once, at :data:`_PRESENCE_ASK_ESCALATE_AT`: the machine has had
    every chance the ladder allows, so what remains is a human's problem. Before that the
    asks are quiet, and after it they continue quietly — a permanently unknowable dialect
    (A1/P1S state 3) still costs one toast per episode, not an hourly one.

    A REFUSED request spends its rung deliberately (pacing and disconnection are the very
    conditions the next rung exists to outlast); the ladder retries, so nothing is lost.
    """
    key = (printer_id, ams_id, tray_id)
    if (
        assignment is None
        or assignment.pre_configured_at is not None
        or (assignment.spool is not None and assignment.spool.spent_at is not None)
    ):
        _presence_stale_episodes.pop(key, None)
        return
    # The canonical tri-state rule, never a re-derivation from tray_type emptiness.
    presence = tray_presence_from_dict(tray)
    if presence is True:
        # The slot answered PRESENT: the claim is checkable again and whatever episode
        # was open is over (a later stale reading is a new situation, timed from then).
        _presence_stale_episodes.pop(key, None)
        return

    episode = _presence_stale_episodes.get(key)
    if episode is None or episode.presence != presence or episode.spool_id != assignment.spool_id:
        episode = _PresenceStaleEpisode(
            now, presence, assignment.spool_id, asks=0, next_ask_at=now + _BOUND_PRESENCE_STALE_AFTER_S
        )
        _presence_stale_episodes[key] = episode
    if now < episode.next_ask_at:
        return

    # Schedule the NEXT rung before doing anything that can raise or be refused: neither
    # outcome may turn a paced ask into a per-pass one.
    asks = episode.asks + 1
    gap = _PRESENCE_ASK_GAPS_S[asks - 1] if asks <= len(_PRESENCE_ASK_GAPS_S) else _PRESENCE_ASK_INTERVAL_S
    _presence_stale_episodes[key] = episode._replace(asks=asks, next_ask_at=now + gap)

    reading = "unknown" if presence is None else "asserted-empty"
    if asks == _PRESENCE_ASK_ESCALATE_AT:
        logger.warning(
            "Bound slot presence stale (%s) for %.0fs after %d requests: printer %d AMS%d-T%d (spool %s) — "
            "asking again and flagging the slot for the operator",
            reading,
            now - episode.first_seen,
            asks - 1,
            printer_id,
            ams_id,
            tray_id,
            assignment.spool_id,
        )
        await ams_presence.broadcast_standing_unknown(db, printer_id, ams_id, tray_id, case="bound_presence_unknown")
    else:
        logger.info(
            "Bound slot presence stale (%s) for %.0fs: printer %d AMS%d-T%d (spool %s) — requesting a fresh report (#%d)",
            reading,
            now - episode.first_seen,
            printer_id,
            ams_id,
            tray_id,
            assignment.spool_id,
            asks,
        )
    manager.request_evidence_pushall(printer_id, "bound_presence_unknown")


async def _age_spent_swap_park(
    db: AsyncSession,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tray: dict,
    assignment: SpoolAssignment | None,
    now: float,
) -> None:
    """Make the spent-swap park LOUD. Sibling of :func:`_age_bound_presence_stale`, not a
    branch of it.

    THE PARK. ``slot_state`` row 4a releases the W1 spent latch on a QUALIFIED PHYSICAL
    CYCLE, and 4a′/5a release it on an ANSWERED no-tag read over a binding that CLAIMS a
    tag. A spent binding sitting under a CONFIGURED, seated tray with neither of those can
    reach neither: the cycle already happened (or never will), and for a TAGLESS incumbent
    the answered-read escape does not exist by design — over a binding that claims no
    identity a no-tag read proves nothing, because the same bare core reads identically
    before and after a swap (doctrine rule 11's one-way clause, scenario G9). So the row
    returns ``KEEP("spent_latch")`` and waits for evidence that may never arrive, on a
    production slot, forever and silently. That silence is the defect; the latch is not.

    WHY A SIBLING, AND NOT AN ARM OF ``_age_bound_presence_stale``. That function's
    predicate is the OPPOSITE on all three axes — it ages a NON-spent binding (it pops its
    episode for a spent one) whose presence reads None or False, i.e. a slot whose wire has
    stopped answering. This park is spent, PRESENT and CONFIGURED: the wire is answering
    perfectly. Bolting one onto the other would put two unrelated situations under one
    name and one episode map, which is the ``_is_tagless`` mistake in a different costume.
    What IS shared is the thing worth sharing: the operator SURFACE
    (``ams_presence.broadcast_standing_unknown``), so the console keeps one vocabulary for
    "this slot is standing unresolved" instead of growing a second one.

    NO TIMER DECIDES IDENTITY. :data:`_SPENT_SWAP_PARK_AFTER_S` gates only whether a HUMAN
    is asked; the farm still concludes nothing about which roll is seated, and no binding,
    row or gram moves here. That is precisely the carve-out doctrine rule 6 makes — a
    duration may gate an action whose false-positive cost is trivial, and the cost of one
    toast on a slot that was about to resolve itself is a toast. The operator's exit is
    already built: "Re-check slot" concludes the swap in one click (rule 12, scenario R5).

    ONE SURFACE PER EPISODE. An episode is one continuous run of the same spent row parked
    under a seated configured tray; it closes when the binding changes, the row stops being
    spent, the tray stops reading present+configured, or a qualified cycle arrives (any of
    which means the park is over and a later one is a NEW situation, timed from then).
    Unlike the presence ladder there is nothing to re-ask the printer for — the wire has
    already said everything it can — so there is no backoff ladder here, only the single
    escalation the ladder exists to reach.
    """
    key = (printer_id, ams_id, tray_id)
    spool = assignment.spool if assignment is not None else None
    parked = (
        assignment is not None
        and spool is not None
        and spool.is_finished_roll
        and assignment.pre_configured_at is None
        # The canonical tri-state rule, never a re-derivation from tray_type emptiness.
        and tray_presence_from_dict(tray) is True
        and bool((tray.get("tray_type") or "").strip())
        # A pending cycle IS the release row 4a is waiting for, and it fires on the next
        # push — so a slot holding one is not parked, it is one push from resolving. PEEK
        # only: consuming it here would destroy the very evidence that ends the park.
        and not qualified_cycle_pending(printer_id, ams_id, tray_id)
    )
    if not parked:
        _spent_swap_park_episodes.pop(key, None)
        return

    episode = _spent_swap_park_episodes.get(key)
    if episode is None or episode.spool_id != assignment.spool_id:
        _spent_swap_park_episodes[key] = _SpentSwapParkEpisode(now, assignment.spool_id, alerted=False)
        return
    if episode.alerted or (now - episode.first_seen) < _SPENT_SWAP_PARK_AFTER_S:
        return

    # Stamp BEFORE the surface: a broadcast that raises or is refused must not turn a
    # once-per-episode alert into a once-per-pass one (the same ordering the presence
    # ladder uses for its rung).
    _spent_swap_park_episodes[key] = episode._replace(alerted=True)
    logger.warning(
        "Spent spool %d has held printer %d AMS%d-T%d for %.0fs under a seated, configured tray with no "
        "qualified roll cycle and no answered read to release it — the slot is parked. Telling the operator "
        "(Re-check slot concludes it in one click).",
        assignment.spool_id,
        printer_id,
        ams_id,
        tray_id,
        now - episode.first_seen,
    )
    await ams_presence.broadcast_standing_unknown(db, printer_id, ams_id, tray_id, case="spent_swap_park")


async def reconcile_slot_config(db: AsyncSession, *, manager=printer_manager, now: float | None = None) -> int:
    """Re-drive slot config the firmware never applied — the DURABLE retry lane.

    Incident (2026-07-24): three slots (printer 3 AMS0-T0/T2, printer 5 AMS0-T3)
    held minted+assigned ledger rows against a BLANK firmware tray config for 15+
    minutes until a human intervened. Their ``ams_filament_setting`` writes had been
    refused by the wire-safety gates (per-printer identify gate / drying), and
    nothing retried: every re-push trigger hangs off ``main.on_ams_change``, which
    ``bambu_mqtt`` fires ONLY when its AMS state hash changes (the deliberate
    anti-storm gate). Once the AMS settled, the hash froze, the callback stopped
    firing, and the refused write was simply lost. The RFID K-profile re-apply has
    the same shape — one change-gated caller, fire-and-forget publish.

    This is the state-derived backstop for both. Each scheduler tick it walks the
    live merged AMS of every active printer and re-drives the owning service
    functions: :func:`maybe_autoconfigure_bare_tray` for a still-bare slot,
    :func:`maybe_harmonize_backup_identity` for a CONFIGURED tagless slot whose
    identity has drifted off the tagless default (the 010-H2S backup-group split —
    the bare arm's sibling on the opposite predicate, sharing its ownership test,
    cadence, epoch ladder and busy backoff),
    ``spool_tag_matcher.reapply_k_profile_if_drifted`` for a Bambu-tagged bound one,
    and ``ams_presence.maybe_command_owed_identify`` for a slot whose owed DISCOVERY
    read the event-driven identify lanes never got to (2026-07-25: six hours behind a
    permanently-engaged extruder). A fourth arm watches PRESENCE rather than identity —
    :func:`_age_bound_presence_stale` ages a bound slot whose merged presence has stopped
    being a checkable claim and, once per episode, asks the printer and the operator to
    settle it. A fifth — :func:`_age_spent_swap_park` — is its sibling on the opposite
    predicate: a SPENT binding under a tray that IS answering (present and configured) with
    no release evidence the decision table may act on, which parks the slot silently
    forever. All of them keep every guard they carry (RFID
    early-exit, spent latch, identify/drying defer, mint settle, config settle,
    operator/RFID-bound never-overwrite, wire-safety refusals, their own retry
    windows) — this lane supplies the missing OCCASION to retry, never a new
    permission. Nothing is remembered between passes beyond the throttle stamp: the
    work is re-derived from live merged state plus the DB, so it is restart-safe by
    construction.

    One backoff on top (2026-07-27): while the printer is RUNNING/PAUSE the AMS
    ignores config writes, so the bare-tray arm keeps the occasion but skips the
    publish (:data:`_CONFIG_PUSH_BUSY_STATES`) instead of re-firing every 30 s for
    hours. Its retry window is left unburned, so the first settled pass re-pushes.

    ``manager``/``now`` are injectable for tests. Returns the number of publishes
    attempted.
    """
    global _last_reconcile_at
    now = monotonic() if now is None else now
    if _last_reconcile_at is not None and (now - _last_reconcile_at) < _RECONCILE_MIN_INTERVAL_S:
        return 0
    _last_reconcile_at = now

    from backend.app.api.routes.settings import get_setting

    # Spoolman owns AMS slots in its mode — main.on_ams_change gates the whole
    # tagless block the same way, and this lane must not reach past that gate.
    spoolman = await get_setting(db, "spoolman_enabled")
    if spoolman is not None and spoolman.lower() == "true":
        return 0

    from backend.app.models.printer import Printer  # local import: cycle avoidance

    # ONE settings read per pass for the walk's ADOPTION test (A.5): a configured slot
    # whose live tray is off-canonical has NOT adopted our config, and calling it adopted
    # would re-arm its write epoch every 30 s against a firmware that keeps refusing —
    # the shape-28 storm, re-created from the other end. The arms below read the setting
    # themselves; they run for at most a handful of slots per pass.
    tagless_default = await _tagless_default(db)

    pushed = 0
    printer_ids = (await db.execute(select(Printer.id).where(Printer.is_active.is_(True)))).scalars().all()
    for printer_id in printer_ids:
        state = manager.get_status(printer_id)
        if state is None or not getattr(state, "raw_data", None):
            continue  # disconnected / never connected — no live slots to reconcile
        ams = state.raw_data.get("ams", [])
        if isinstance(ams, dict):
            ams = ams.get("ams", [])
        if not ams:
            continue
        res = await db.execute(
            select(SpoolAssignment)
            .options(selectinload(SpoolAssignment.spool))
            .where(SpoolAssignment.printer_id == printer_id)
        )
        bound = {(a.ams_id, a.tray_id): a for a in res.scalars().all()}
        # A printer that is RUNNING or PAUSEd has a busy AMS that silently drops
        # config writes; the bare-tray arm backs off on it (see below). The K-drift
        # arm keeps its own gates and the owed-identify arm already self-refuses
        # mid-print, so neither is touched here.
        busy_state = (getattr(state, "state", None) or "").upper()
        printer_busy = busy_state in _CONFIG_PUSH_BUSY_STATES
        # At most ONE commanded discovery read per printer per pass: the client's
        # per-printer identify gate would refuse the rest anyway, and provoking that
        # refusal from our own loop is how a lane starts logging WARNINGs about itself.
        identify_spent = False

        for unit in ams:
            if not isinstance(unit, dict):
                continue
            try:
                ams_id = int(unit.get("id", -1))
            except (TypeError, ValueError):
                continue
            for tray in unit.get("tray", []) or []:
                if not isinstance(tray, dict):
                    continue
                try:
                    tray_id = int(tray.get("id", -1))
                except (TypeError, ValueError):
                    continue
                tag_uid = tray.get("tag_uid", "") or ""
                tray_uuid = tray.get("tray_uuid", "") or ""
                assignment = bound.get((ams_id, tray_id))
                # Per-slot guard: one wedged slot must not strand the rest of the
                # fleet's reconcile for the whole pass (the tick's own guard would
                # abandon every printer after this one).
                try:
                    configured = bool((tray.get("tray_type") or "").strip())
                    own_tagless = configured and _own_tagless_slot(tray, assignment)
                    if configured and (not own_tagless or _tray_canonical_delta(tagless_default, tray) is None):
                        # ADOPTION — and this walk is its RELIABLE observer: both
                        # bare-tray call sites pre-filter on an empty tray_type, so a
                        # slot that has taken the config stops reaching the lane
                        # entirely and would otherwise carry its strike count into a
                        # later bare episode.
                        #
                        # Adoption is DERIVED, never stored (2026-08-21): a non-empty
                        # tray_type proves the firmware applied SOMETHING, which is the
                        # whole answer for a slot that is not ours to canonicalise, but
                        # for one that IS, the identity it applied has to be the one we
                        # asked for. A tray still reading its own colour has not adopted
                        # our config, and marking it adopted would re-arm the harmonise
                        # arm's epoch every pass — an unbounded write loop against a
                        # refusing firmware, which is the exact class the epoch ladder
                        # exists to bound.
                        note_config_adopted(printer_id, ams_id, tray_id)

                    if tray_present(tray) and not configured and not is_valid_tag(tag_uid, tray_uuid):
                        # BARE — the incident shape. The callee owns every guard,
                        # including the operator/RFID-bound never-overwrite: a config
                        # write racing a firmware tag read is the 2026-07-18 HMS
                        # 0700_0081 class, so the re-push stays ams_auto-only.
                        if printer_busy:
                            # BUSY BACKOFF (2026-07-27, 002-T1): a printing/paused AMS
                            # ignores config writes, and this lane re-fired every 30 s
                            # for FOUR HOURS (998 log lines) without converging. Skip
                            # the PUBLISH only — the retry window is deliberately left
                            # unburned (the callee is what stamps it), so the very
                            # first pass after the printer settles re-pushes
                            # immediately instead of waiting out the cadence. Same
                            # "supply the occasion, never a new permission" contract
                            # as the identify/drying defers.
                            logger.debug(
                                "Slot-config reconcile: printer %d AMS%d-T%d bare-tray re-push skipped "
                                "— printer is %s (a busy AMS ignores config writes)",
                                printer_id,
                                ams_id,
                                tray_id,
                                busy_state,
                            )
                        elif await maybe_autoconfigure_bare_tray(db, printer_id, ams_id, tray_id, tray):
                            pushed += 1
                    elif (
                        assignment is not None
                        and assignment.spool is not None
                        and is_bambu_tag(tag_uid, tray_uuid, tray.get("tray_info_idx", ""))
                    ):
                        # TAGGED — drifted calibration. Internally rate-limited and
                        # cheap when the spool carries no stored K-profile.
                        if await reapply_k_profile_if_drifted(
                            db, printer_id, ams_id, tray_id, tray, assignment.spool, state
                        ):
                            pushed += 1
                    elif own_tagless:
                        # CONFIGURED but off-canonical — the 010-H2S backup-group split.
                        # Same busy backoff as the bare arm and for the same reason: a
                        # RUNNING/PAUSE AMS silently drops config writes, so skip the
                        # PUBLISH and leave the retry window unburned rather than
                        # re-firing every 30 s for hours. The callee owns every other
                        # guard, and refuses in silence when the slot is already a peer.
                        if printer_busy:
                            logger.debug(
                                "Slot-config reconcile: printer %d AMS%d-T%d backup-group harmonise skipped "
                                "— printer is %s (a busy AMS ignores config writes)",
                                printer_id,
                                ams_id,
                                tray_id,
                                busy_state,
                            )
                        elif await maybe_harmonize_backup_identity(db, printer_id, ams_id, tray_id, tray, assignment):
                            pushed += 1

                    # OWED DISCOVERY READ — the identity twin of the config re-push
                    # above. A slot the farm knows changed but has never identified is
                    # exactly what makes every config decision for it a guess (and what
                    # holds the settle gate open). Both event-driven identify lanes can
                    # miss it forever on a busy printer, so this state-derived pass is
                    # the drain. The callee owns every guard (need, running, engaged,
                    # drying, client refusal) — this supplies only the occasion.
                    if not identify_spent and await ams_presence.maybe_command_owed_identify(
                        db, printer_id, ams_id, tray_id, tray, state
                    ):
                        identify_spent = True

                    # BOUND-BUT-PRESENCE-STALE — the release lane's liveness probe, and
                    # the fourth thing this walk is durable for. The arms above chase an
                    # unknown IDENTITY; this one chases a stale PRESENCE, which is what a
                    # binding is actually a claim about (doctrine rule 9).
                    await _age_bound_presence_stale(db, printer_id, ams_id, tray_id, tray, assignment, now, manager)

                    # SPENT-SWAP PARK — the arm above's sibling, on the opposite
                    # predicate: a spent binding under a tray that is answering
                    # perfectly (present AND configured) and has no release evidence
                    # the decision table can act on. Nothing to ask the printer for
                    # here, so it asks the OPERATOR, once per episode.
                    await _age_spent_swap_park(db, printer_id, ams_id, tray_id, tray, assignment, now)
                except Exception:  # noqa: BLE001 — one bad slot must not abort the pass
                    logger.exception(
                        "Slot-config reconcile failed for printer %d AMS%d-T%d",
                        printer_id,
                        ams_id,
                        tray_id,
                    )

    # SPENT-CONTRADICTION sweep — this walk's fifth duty, and the only one that checks a
    # DURABLE claim against the wire rather than re-driving a write. A spool stamped
    # spent is hard-excluded from every print, so a false stamp silently retires a
    # healthy roll; spools 185/205 sat spent-and-loaded-and-full for nine days because
    # nothing compared the two. It rides HERE because this is already the lane that
    # holds live merged AMS state beside the DB every tick. No guard at this call site
    # by design: the detector is an entry hook that owns its own throttle (its own
    # 15-minute floor, far slower than this pass) and its own guard, and it never
    # mutates a spool row.
    await spool_respool.detect_spent_contradictions(db, manager)
    return pushed


# --- dedup lifecycle -------------------------------------------------------


def clear_autoconfig_dedup(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Drop the bare-tray retry timestamp AND write epoch for a slot (it emptied).

    An empty slot is a presence EDGE: whatever the wire refused to configure has left,
    so both gates that would hold the next roll's first write back are dropped together
    — the cadence stamp and the epoch latch. Splitting them would leave a slot re-armed
    for cadence but still latched, i.e. silently unconfigurable for the next roll.
    """
    _autoconfig_window.clear((printer_id, ams_id, tray_id))
    _rearm_autoconfig_epoch(printer_id, ams_id, tray_id, cause="the slot reading empty")


# --- provisional disposal on RFID takeover ---------------------------------


async def dispose_provisional_on_tag(db: AsyncSession, spool: Spool | None) -> str:
    """Dispose an auto-minted tagless row when a real RFID tag claims its slot.

    Hard-delete a pristine provisional row (no ``SpoolUsageHistory``) or archive a
    ledger-bearing one — mirrors ``spool_respool``'s donor disposition. Returns
    the disposition ("hard-deleted" / "archived" / "kept"). "kept" means the
    spool was not an auto-minted provisional row and must be left untouched.
    """
    if spool is None or spool.data_origin != DATA_ORIGIN:
        return "kept"
    history_count = await db.scalar(
        select(func.count(SpoolUsageHistory.id)).where(SpoolUsageHistory.spool_id == spool.id)
    )
    if not history_count:
        await db.delete(spool)
        return "hard-deleted"
    spool.archived_at = datetime.utcnow()
    return "archived"
