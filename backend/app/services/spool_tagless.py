"""Tagless (non-RFID) spool support lanes — wire config, minting, operator verbs.

**Scope after the W3b cutover.** Deciding WHAT is in a tray and WHICH ledger row it
is belongs to ``slot_state`` (the decision table) and ``slot_pipeline`` (the one
orchestrator). This module no longer decides identity: its 7-branch
``handle_tagless_slot`` tree, the in-place ``_maybe_move_tagless_assignment`` rebind
and the terminal-time identity reconcile were deleted, not re-homed. What remains is
everything AROUND that decision, in three groups:

* **Minting + tagless defaults** — :func:`mint_tagless_spool`,
  :func:`tagless_default_filament`, :func:`override_generic_identity`. The pipeline
  calls these to execute a MINT it decided on; the shapes are unchanged.
* **Wire config (a sibling lane, NOT identity)** — :func:`maybe_autoconfigure_bare_tray`
  pushes the default filament to a BARE tray (spool present, ``tray_type`` empty,
  state 10/11) so the slot is usable, including mid-print where it joins the firmware
  backup pool (decision D3b). The pipeline classifies a bare tray
  ``OCCUPIED_UNRESOLVED`` and mutates no binding; this lane gives the firmware a
  configuration so the NEXT push carries a filament type and enters the pipeline's
  tagless lane. :func:`reconcile_slot_config` is its durable scheduler-tick retry lane
  (plus K-drift and owed-identify arms), because ``main.on_ams_change`` is
  change-gated and a settled AMS stops firing it (2026-07-24 incident).
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

Module edge state (``_autoconfig_window``, ``_pending_physical_cycles``) mirrors the
fork's other event-edge bookkeeping (``spool_respool._last_tray_now``). It is lost on
restart — worst case a bare-tray config re-push waits one AMS push and a spent slot
stays latched until a pull/reseat (honest, not silent). The fresh-roll prompt is
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

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from backend.app.core.websocket import ws_manager
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import ams_presence, spool_respool
from backend.app.services.printer_manager import printer_manager
from backend.app.services.spool_binding import bind_spool_to_slot
from backend.app.services.spool_tag_matcher import (
    auto_assign_spool,
    is_bambu_tag,
    is_valid_tag,
    parse_tray_fields,
    reapply_k_profile_if_drifted,
)
from backend.app.services.tray_fields import tray_presence_from_dict
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
_pending_physical_cycles: set[tuple[int, int, int]] = set()

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
    looking at one unchanging situation and may act on it exactly once (``fired``).
    ``first_seen`` is when that situation started, i.e. what :data:`_BOUND_PRESENCE_STALE_AFTER_S`
    is measured against.
    """

    first_seen: float
    presence: bool | None
    spool_id: int
    fired: bool


# (printer_id, ams_id, tray_id) -> the open episode for that slot, or absent when the
# slot is healthy/ineligible. Session state by design — a restart re-derives everything
# from the next pass's live state and the DB.
_presence_stale_episodes: dict[tuple[int, int, int], _PresenceStaleEpisode] = {}

# How long ONE presence-stale reading must stand before the farm stops waiting for the
# wire to volunteer an answer. Deliberately well past every ORDINARY stale window: mid-
# print the H2S reduces its tray blocks to presence-unknown partials for the whole job,
# and eject/settle gaps are minutes at most. Past 15 minutes the reports have simply
# stopped carrying what the binding needs, and the two parties who can supply it — the
# printer (a fresh full report) and the operator (a standing-unknown toast) — are asked.
_BOUND_PRESENCE_STALE_AFTER_S: float = 900.0

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
    _pending_physical_cycles.clear()
    _presence_stale_episodes.clear()
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
    """True when the spool carries no RFID identity (no tag_uid and no tray_uuid)."""
    if spool is None:
        return False
    return not (spool.tag_uid or spool.tray_uuid)


def fingerprint_matches(spool: Spool, tray: dict) -> bool:
    """Same physical filament: color within tolerance AND same canonical material."""
    if not colors_similar(tray.get("tray_color") or "", spool.rgba or "FFFFFFFF"):
        return False
    return canonical_filament_type(_tray_material(tray)) == canonical_filament_type(spool.material or "")


def _fingerprint_matches_default(material: str | None, rgba: str | None, default: dict) -> bool:
    """True when a (material, rgba) pair fingerprint-matches the tagless default —
    same canonical material AND color within tolerance. The material+rgba twin of
    :func:`fingerprint_matches` (a dict default has no tray shape), shared by the
    generic-id mint override (W4.4) and :func:`default_temps_for_fingerprint`."""
    if canonical_filament_type(material or "") != canonical_filament_type(default.get("material") or ""):
        return False
    return colors_similar(rgba or "", default.get("rgba") or "")


async def default_temps_for_fingerprint(
    db: AsyncSession, material: str | None, rgba: str | None
) -> tuple[int, int] | None:
    """The tagless default's ``(nozzle_temp_min, nozzle_temp_max)`` IFF a
    (material, rgba) pair fingerprint-matches the configured default AND the default
    carries both temps; else ``None``.

    Public accessor over the single tagless-default JSON parser (:func:`_tagless_default`)
    — the slicer resolver's middle nozzle-temp tier (row temps → THIS →
    ``MATERIAL_TEMPS``) so a fingerprint-matched tagless slot inherits the default's
    canonical range and stays a byte-identical firmware backup-group peer (W4)."""
    default = await _tagless_default(db)
    if default is None or not _fingerprint_matches_default(material, rgba, default):
        return None
    tmin = default.get("nozzle_temp_min")
    tmax = default.get("nozzle_temp_max")
    if tmin is None or tmax is None:
        return None
    try:
        return (int(tmin), int(tmax))
    except (TypeError, ValueError):
        return None


async def override_generic_identity(
    db: AsyncSession, slicer_filament: str | None, material: str | None, rgba: str | None
) -> dict | None:
    """The tagless default's SPECIFIC identity to write instead of a GENERIC one.

    Returns ``{"slicer_filament", "nozzle_temp_min", "nozzle_temp_max"}`` when
    ``slicer_filament`` is a generic id (``GFG99`` …) AND the configured tagless
    default carries a specific id AND ``(material, rgba)`` fingerprint-matches that
    default; ``None`` otherwise (nothing to override).

    Generic-id self-perpetuation is the 011-H2S no-auto-refill cause (2026-07-19):
    the firmware's auto-refill backup group only pairs slots whose brand-class /
    type / colour / nozzle temps match EXACTLY, so one slot configured ``GFG99``
    beside a ``GFG02`` peer splits the group. A generic id enters the ledger from a
    bare-tray auto-config or a legacy row and is then re-read and re-published
    forever. This is the single override, consumed at BOTH ends of that loop: the
    tagless mint (:func:`mint_tagless_spool`, so a re-read row stops carrying it)
    and the wire resolver (``slicer_filament_resolver.resolve_slicer_filament``, so
    a stale row already in the DB can no longer re-publish it). The temps ride along
    deliberately — substituting the id while keeping a stale row's temps would still
    split the group on the temperature dimension.
    """
    if not slicer_filament or slicer_filament not in _GENERIC_ID_VALUES:
        return None
    default = await _tagless_default(db)
    if default is None:
        return None
    specific = default.get("slicer_filament") or ""
    if not specific or not _fingerprint_matches_default(material, rgba, default):
        return None
    return {
        "slicer_filament": specific,
        "nozzle_temp_min": default.get("nozzle_temp_min"),
        "nozzle_temp_max": default.get("nozzle_temp_max"),
    }


# --- setting helpers --------------------------------------------------------


async def _auto_add_untagged(db: AsyncSession) -> bool:
    from backend.app.api.routes.settings import get_setting

    raw = await get_setting(db, "auto_add_untagged")
    return raw is None or raw.lower() == "true"


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
        # Generic-id self-perpetuation guard (W4.4): a tray re-reporting the GENERIC
        # id an earlier bare-tray auto-config wrote mints the tagless default's
        # SPECIFIC id/name/temps instead — see :func:`override_generic_identity`.
        _override = await override_generic_identity(db, slicer_filament, material, rgba)
        if _override is not None:
            slicer_filament = _override["slicer_filament"]
            slicer_filament_name = None
            nozzle_temp_min = _override["nozzle_temp_min"]
            nozzle_temp_max = _override["nozzle_temp_max"]
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
    """
    if _printer_busy(printer_id, on_error=False):
        return False  # mid-print: no firmware auto-read exists to protect
    gain_age = ams_presence.recent_gain_age(printer_id, ams_id, tray_id)
    if gain_age is not None and gain_age < _CONFIG_SETTLE_S:
        return True
    if not ams_presence.identity_unanswered(printer_id, ams_id, tray_id):
        return False
    cycle_age = ams_presence.last_physical_cycle_age(printer_id, ams_id, tray_id)
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
    """Apply the tagless-fresh route's optional manual fields onto a fresh row.

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
    :func:`maybe_autoconfigure_bare_tray` and the W5 tagless-fresh route
    (:func:`apply_fresh_roll` — the "New roll" verb). The WIRE lane's equivalent is the
    pipeline's ``REPLACE_SPENT`` arm (``slot_pipeline._apply_replace_spent``), which
    mirrors this behaviour with one documented difference (it disposes a pristine
    provisional row instead of archiving an empty husk); the two must stay aligned.
    Default-mints from the configured tagless default
    when the tray is bare/absent OR still carries the departed row's config (firmware
    leftover — :func:`fingerprint_matches`), so a physically-fresh roll gets a clean
    4-dimension identity; else mints from the tray's own (genuinely different) config.
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
    via the tagless-fresh route). Idempotent: an already-clear row is a no-op, so a
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
    prompt). A NON-spent row whose tray is STILL PRESENT and is consumed past
    :data:`_FRESH_ROLL_PROMPT_USED_FRAC` of its label broadcasts ``tagless_fresh_prompt``
    and stamps ``fresh_prompt_pending_at``. Every non-spent outcome (prompt, absent tray,
    or sub-threshold) POPs the pending cycle — no latch is involved for non-spent rows.

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
    if spool is None or not is_tagless_spool(spool):
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
    bare-tray transitions perform, exposed for the slot pipeline's REPLACE_SPENT arm.
    """
    key = (printer_id, ams_id, tray_id)
    if key not in _pending_physical_cycles:
        return False
    _pending_physical_cycles.discard(key)
    return True


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


# --- D3b: bare-tray auto-config --------------------------------------------


async def maybe_autoconfigure_bare_tray(
    db: AsyncSession, printer_id: int, ams_id: int, tray_id: int, tray: dict, *, force: bool = False
) -> bool:
    """Push a default filament to a BARE tray (spool present, nothing configured).

    Makes an unconfigured third-party spool usable — including mid-print, where
    the newly-configured slot joins the firmware backup pool. Returns True when a
    config push was attempted this tick.

    Trigger: tray PRESENT (state 10/11) AND tray_type empty AND no valid tag AND
    ``auto_add_untagged`` AND a non-empty ``tagless_default_filament`` setting.
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
    """
    if not tray_present(tray):
        return False
    if (tray.get("tray_type") or "").strip():
        return False  # already configured — not bare
    if is_valid_tag(tray.get("tag_uid", "") or "", tray.get("tray_uuid", "") or ""):
        return False  # RFID tray — not tagless
    if not await _auto_add_untagged(db):
        return False
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

    if assignment is not None and (assignment.spool is None or assignment.spool.data_origin != DATA_ORIGIN):
        # Operator- or RFID-bound slot (or an orphan) — never overwrite. Only our
        # OWN auto-minted default is eligible for a self-healing re-push.
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

    **EPISODE SEMANTICS.** An episode is one continuous run of the same reading on the
    same binding, and the farm acts on it exactly ONCE, past
    :data:`_BOUND_PRESENCE_STALE_AFTER_S`. It closes only when the presence VALUE changes
    (including None↔False) or the binding does — never on a timer. The reason is that a
    repeat ask answers nothing: if the requested report arrived and the reading still
    stands, the wire has said "this IS my answer", and asking again is noise against a
    printer that is already telling the truth. It is also what keeps a permanently
    unknowable dialect (A1/P1S state 3) to a single lifetime signal per slot instead of
    an hourly toast forever. A refused request is likewise not retried within the
    episode — a disconnected printer re-pushes a full report on reconnect anyway, which
    moves the reading and opens a fresh episode on its own.
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
        episode = _PresenceStaleEpisode(now, presence, assignment.spool_id, False)
        _presence_stale_episodes[key] = episode
    if episode.fired or now - episode.first_seen < _BOUND_PRESENCE_STALE_AFTER_S:
        return

    # Spend the episode BEFORE the escalations: neither of them raising (nor a refused
    # request) may turn a one-shot ask into a per-pass one.
    _presence_stale_episodes[key] = episode._replace(fired=True)
    logger.warning(
        "Bound slot presence stale (%s) for %.0fs: printer %d AMS%d-T%d (spool %s) — "
        "requesting a fresh report and flagging the slot",
        "unknown" if presence is None else "asserted-empty",
        now - episode.first_seen,
        printer_id,
        ams_id,
        tray_id,
        assignment.spool_id,
    )
    await ams_presence.broadcast_standing_unknown(db, printer_id, ams_id, tray_id, case="bound_presence_unknown")
    manager.request_evidence_pushall(printer_id, "bound_presence_unknown")


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
    ``spool_tag_matcher.reapply_k_profile_if_drifted`` for a Bambu-tagged bound one,
    and ``ams_presence.maybe_command_owed_identify`` for a slot whose owed DISCOVERY
    read the event-driven identify lanes never got to (2026-07-25: six hours behind a
    permanently-engaged extruder). A fourth arm watches PRESENCE rather than identity —
    :func:`_age_bound_presence_stale` ages a bound slot whose merged presence has stopped
    being a checkable claim and, once per episode, asks the printer and the operator to
    settle it. All of them keep every guard they carry (RFID
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
                    if (
                        tray_present(tray)
                        and not (tray.get("tray_type") or "").strip()
                        and not is_valid_tag(tag_uid, tray_uuid)
                    ):
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
    """Drop the bare-tray retry timestamp for a slot (called when it empties)."""
    _autoconfig_window.clear((printer_id, ams_id, tray_id))


# --- provisional disposal on RFID takeover ---------------------------------


async def dispose_provisional_on_tag(db: AsyncSession, spool: Spool | None) -> str:
    """Dispose an auto-minted tagless row when a real RFID tag claims its slot.

    Hard-delete a pristine provisional row (no ``SpoolUsageHistory``) or archive a
    ledger-bearing one — mirrors ``spool_respool``'s donor disposition. Returns
    the disposition ("hard-deleted" / "archived" / "kept"). "kept" means the
    spool was not an auto-minted provisional row and must be left untouched.
    """
    from backend.app.models.spool_usage_history import SpoolUsageHistory

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
