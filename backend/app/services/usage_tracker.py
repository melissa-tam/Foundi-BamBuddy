"""Automatic filament consumption tracking.

Captures AMS tray remain% at print start, then computes consumption
deltas at print complete to update spool weight_used and last_used.

Primary tracking uses 3MF slicer estimates (precise per-filament data).
AMS remain% delta is the fallback for trays not covered by 3MF data.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services.tray_fields import normalized_tag_uid, normalized_tray_uuid
from backend.app.utils.tag_normalization import tag_matches_row

logger = logging.getLogger(__name__)

# gcode_state values that mean a print is actively running (or about to feed
# filament). Folding AMS remain% into weight_used during any of these
# double-counts this module's precise per-print 3MF deduction (#880).
_ACTIVE_PRINT_GCODE_STATES = ("RUNNING", "PAUSE", "PREPARE", "SLICING")

# Durable dedup for the zero-gram page (scenario C6). The WARN log is the per-print
# RECORD and is never suppressed; the notification is the PAGE, and one page per
# printer per window is what an operator can act on. This class of bleed repeats on
# every dispatch — fifteen prints on 2026-08-18 — so a page per print would bury the
# very fact it exists to surface.
_ZERO_CHARGE_SCOPE = "zero_gram_charge"
_ZERO_CHARGE_RENOTIFY_S = 6 * 3600.0

# Which witnesses ``_print_feeder_keys`` may consult — see that function.
Witnesses = Literal["all", "dispatch_only"]


async def ams_weight_sync_allowed(db: AsyncSession, printer_id: int, state) -> bool:
    """Whether a caller may fold this printer's AMS remain% into spool
    ``weight_used`` right now.

    The precise per-print deduction is this module's job at completion (3MF /
    G-code). Folding the low-resolution AMS remain% in WHILE a print is active
    double-counts (#880). The former guard snapshotted the in-memory
    ``_active_sessions`` set, which a server restart mid-print wiped — the sync
    then ran against a live print and double-counted. Both legs here are durable:

    - the live gcode_state is a settled (non-active) one — a missing / ``unknown``
      state fails closed (possibly-active, sync NOT allowed); and
    - no PrintArchive for this printer is still ``status="printing"`` (covers a
      live state that lags the DB).

    Lives here (with the deduction it protects) rather than in ``main``: both the
    push-driven sync in ``main.on_ams_change`` and the manual
    ``POST /inventory/sync-ams-weights`` recovery tool gate on this one origin.
    """
    if state is None:
        return False
    live_state = (getattr(state, "state", None) or "").upper()
    if not live_state or live_state == "UNKNOWN" or live_state in _ACTIVE_PRINT_GCODE_STATES:
        return False

    from backend.app.models.archive import PrintArchive

    result = await db.execute(
        select(PrintArchive.id)
        .where(PrintArchive.printer_id == printer_id)
        .where(PrintArchive.status == "printing")
        .limit(1)
    )
    return result.scalar_one_or_none() is None


# ── Tagged-ledger DECREASE reconcile (W6) ────────────────────────────────────
#
# The push-driven weight sync in ``main.on_ams_change`` is INCREASE-ONLY by
# design: the AMS remain% is integer-resolution (10 g steps on a 1 kg roll) and
# must never overwrite the precise 3MF/G-code deduction computed here. The cost of
# that rule is that a ledger which over-counts can never heal itself — production
# spool 37 sat at 899 g used against a wire-FULL roll for two weeks (the grams were
# charged while the row was stale-bound to another printer's tray on 07-17), and
# the only bidirectional path was the manual sync tool.
#
# Doctrine rule 8: for a TAGGED row the wire remain IS truth — the firmware read
# the roll's own RFID chip. So a large, stable contradiction in the decrease
# direction is repaired automatically, with a WARNING + an operator notification
# because a silent ledger rewrite is exactly what nobody should ship.
#
# Deliberately NOT a prompt: hardware truth is knowable here (doctrine rule 1).
# Deliberately NOT applied to tagless rows: they have no wire truth to defer to.
_LEDGER_DECREASE_MARGIN_PCT = 50.0

# Corroboration, mirroring ``spool_respool._JUMP_MIN_PUSHES`` / ``_JUMP_STABLE_S``:
# one push is not evidence (the AMS re-reports a tray on every state change), so
# the contradiction must HOLD across at least two pushes spanning ten seconds. A
# push that stops reading as a contradiction drops the window entirely. Process
# lifetime only — a restart simply re-corroborates.
_LEDGER_DECREASE_MIN_PUSHES = 2
_LEDGER_DECREASE_STABLE_S = 10.0

# (printer_id, ams_id, tray_id) -> (first_seen_monotonic, observation_count)
_ledger_decrease_seen: dict[tuple[int, int, int], tuple[float, int]] = {}


def clear_ledger_decrease_window(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Drop a slot's decrease-reconcile corroboration (called on the empty edge).

    An emptied slot invalidates everything learned about the roll that was in it;
    without this, a half-corroborated window could carry across a roll swap and let
    the NEXT roll's first qualifying push fire immediately. Twin of
    ``spool_respool.clear_respool_prompt_dedup``, called from the same edge.
    """
    _ledger_decrease_seen.pop((printer_id, ams_id, tray_id), None)


def _reset_ledger_decrease_state() -> None:
    """Test hook: clear the corroboration ledger between cases."""
    _ledger_decrease_seen.clear()


def wire_identity_is_the_bound_row(spool: Spool, tray: dict) -> bool:
    """True when the roll ON THE WIRE is provably the row we are about to rewrite.

    PUBLIC because it is ONE contract, not one lane's private helper: the decrease
    reconcile below and ``main.on_ams_change``'s increase-only remain% sync are the two
    ledger writers that derive grams from a wire reading, and both must answer this same
    question first. It was private while only the decrease lane asked it, and the cost of
    that asymmetry is the documented spool-37 case at the top of this section — the
    INCREASE lane inflated a stale-bound row to 899 g from another slot's remain%.

    The whole justification for writing a ledger down is "the firmware read THIS
    roll's chip", so the identity must agree before the grams do. UUID-PRIMARY, per
    the corrected 2026-08-01 identity law: a Bambu roll carries TWO RFID tags
    sharing one ``tray_uuid``, so a differing ``tag_uid`` beside an agreeing uuid is
    a sibling read of the same roll — not a swap. Only when neither side asserts a
    uuid does the tag decide, and then it decides against the row's PAIR of chips
    (:func:`tag_matches_row`): a push carrying only the far chip is still this roll
    identifying itself. Comparing the near chip alone silently gated the weight-sync
    and decrease-reconcile lanes OFF for a roll that WAS the bound roll, for as long
    as it sat facing that way.

    Refuses (False) whenever nothing is comparable: a tagless row, an untagged
    tray, or a partial push asserting neither member. Silence is never agreement —
    a slot mid-swap that the pipeline has not resolved yet must not have the
    departing row's ledger rewritten from the arriving roll's remain%.
    """
    wire_uuid = normalized_tray_uuid(tray.get("tray_uuid"))
    row_uuid = normalized_tray_uuid(spool.tray_uuid)
    if wire_uuid is not None and row_uuid is not None:
        return wire_uuid == row_uuid

    wire_tag = normalized_tag_uid(tray.get("tag_uid"))
    row_tag = normalized_tag_uid(spool.tag_uid)
    row_sibling = normalized_tag_uid(spool.sibling_tag_uid)
    if wire_tag is not None and (row_tag is not None or row_sibling is not None):
        return tag_matches_row(wire_tag, row_tag, row_sibling)

    return False


async def maybe_reconcile_tagged_ledger_decrease(
    db: AsyncSession,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tray: dict,
    spool: Spool,
    *,
    sync_allowed: bool,
) -> bool:
    """Write a TAGGED spool's ``weight_used`` DOWN to the wire's remain%.

    Fires only when every gate below holds; returns True iff the ledger was
    written. Internal-inventory mode only — the caller runs inside
    ``on_ams_change``'s Spoolman gate (Spoolman owns its own weights).

    1. ``sync_allowed`` — the caller's ``ams_weight_sync_allowed`` verdict: the
       printer is idle and no archive is still ``printing``. Mid-print the usage
       tracker owns the ledger and the wire lags a live extrusion.
    2. The wire identity IS the bound row (:func:`wire_identity_is_the_bound_row`,
       uuid-primary) — a tagless row has no wire truth to defer to, and a slot
       holding a DIFFERENT roll must never have this row's ledger rewritten from
       it — and the row is not ``weight_locked`` (an operator-pinned weight is
       never overwritten).
    3. The reading is trustworthy (``spool_respool.remain_reading_untrustworthy``:
       no identify in flight, unit not drying) and ``remain`` parses to 1..100.
    4. No re-spool prompt is open on the slot: the operator is already being asked
       whether this tag moved onto a fresh roll, and answering it automatically —
       in the wrong direction — is how a donor's history gets erased.
    5. The contradiction is at least :data:`_LEDGER_DECREASE_MARGIN_PCT`
       points-of-label and has HELD across
       :data:`_LEDGER_DECREASE_MIN_PUSHES` pushes spanning
       :data:`_LEDGER_DECREASE_STABLE_S`.

    Prod acceptance case: spool 37 (label 1000 g, used 899.28 g, wire remain
    100%) → margin ≈ 90 points → ``weight_used`` 899.3 → 0.0 after the second
    stable push.
    """
    from backend.app.services.spool_respool import (
        remain_jump_margin,
        remain_reading_untrustworthy,
        respool_prompt_open_for_slot,
    )

    key = (printer_id, ams_id, tray_id)

    margin = remain_jump_margin(spool, tray)
    if (
        not sync_allowed
        or not wire_identity_is_the_bound_row(spool, tray)
        or spool.weight_locked
        or margin is None
        or margin < _LEDGER_DECREASE_MARGIN_PCT
    ):
        # The condition must HOLD, not merely have happened once.
        _ledger_decrease_seen.pop(key, None)
        return False

    if remain_reading_untrustworthy(printer_id, ams_id, tray_id):
        # Neither fires nor counts — an in-flux reading is not evidence either way.
        return False

    if respool_prompt_open_for_slot(printer_id, ams_id, tray_id):
        logger.debug(
            "[ledger-reconcile] spool %d (printer %d AMS%d-T%d) deferring to an open re-spool prompt",
            spool.id,
            printer_id,
            ams_id,
            tray_id,
        )
        return False

    now = monotonic()
    first_seen, count = _ledger_decrease_seen.get(key, (now, 0))
    count += 1
    _ledger_decrease_seen[key] = (first_seen, count)
    if count < _LEDGER_DECREASE_MIN_PUSHES or (now - first_seen) < _LEDGER_DECREASE_STABLE_S:
        return False

    remain_val = int(tray.get("remain"))
    label_weight = float(spool.label_weight or 0)
    wire_used = round(label_weight * (100 - remain_val) / 100.0, 1)
    current_used = round(float(spool.weight_used or 0), 1)
    if wire_used >= current_used:
        # Margin says decrease but the grams do not (a rounding edge) — nothing to do.
        _ledger_decrease_seen.pop(key, None)
        return False

    logger.warning(
        "[ledger-reconcile] spool %d weight_used %.1f -> %.1f (wire remain %d%% contradicts ledger by %.0f pts; "
        "doctrine rule 8 — wire remain is truth for tagged rows)",
        spool.id,
        current_used,
        wire_used,
        remain_val,
        margin,
    )
    spool.weight_used = wire_used
    await db.commit()
    _ledger_decrease_seen.pop(key, None)

    await _notify_ledger_reconciled(db, printer_id, ams_id, tray_id, spool, current_used, wire_used, remain_val)
    return True


async def _notify_ledger_reconciled(
    db: AsyncSession,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    spool: Spool,
    old_used: float,
    new_used: float,
    remain_val: int,
) -> None:
    """Tell the operator the ledger was rewritten. Never raises.

    Rides the existing AMS-issue channel (``on_printer_error`` — "AMS issues,
    etc.", the same one HMS summaries use) rather than minting a new event type:
    an automatic write to the gram ledger must be visible wherever AMS trouble
    already is, and the notification surface stays as it was configured.
    """
    try:
        from backend.app.models.printer import Printer
        from backend.app.services.notification_service import notification_service

        printer = await db.get(Printer, printer_id)
        printer_name = printer.name if printer else f"Printer {printer_id}"
        detail = (
            f"Spool #{spool.id} on AMS{ams_id}-T{tray_id} read {remain_val}% full on the wire while the "
            f"ledger held {old_used:.1f} g used. The RFID reading is authoritative for a tagged roll, so "
            f"weight_used was corrected to {new_used:.1f} g."
        )
        await notification_service.on_printer_error(
            printer_id,
            printer_name,
            "Spool ledger corrected",
            db,
            detail,
        )
    except Exception:  # noqa: BLE001 — the ledger write already succeeded; never undo it over a notify
        logger.exception(
            "[ledger-reconcile] notification failed for spool %d (printer %d AMS%d-T%d)",
            spool.id,
            printer_id,
            ams_id,
            tray_id,
        )


def _decode_mqtt_mapping(mapping_raw: list | None) -> list[int] | None:
    """Decode MQTT mapping field (snow-encoded) to bambuddy global tray IDs.

    The printer's MQTT mapping field is an array indexed by slicer filament slot
    (0-based). Each value uses snow encoding: ams_hw_id * 256 + local_slot.
    65535 means unmapped.

    Returns a list of bambuddy global tray IDs (or -1 for unmapped), or None if
    no valid mappings found.
    """
    if not isinstance(mapping_raw, list) or not mapping_raw:
        return None

    result = []
    for value in mapping_raw:
        if not isinstance(value, int) or value >= 65535:
            result.append(-1)
            continue

        ams_hw_id = value >> 8
        slot = value & 0xFF

        if 0 <= ams_hw_id <= 3:
            # Regular AMS: sequential global ID
            result.append(ams_hw_id * 4 + (slot & 0x03))
        elif 128 <= ams_hw_id <= 135:
            # AMS-HT: global ID is the hardware ID (one slot per unit)
            result.append(ams_hw_id)
        elif ams_hw_id in (254, 255):
            # External spool
            result.append(254 if slot != 255 else 255)
        else:
            result.append(-1)

    # Only return if at least one valid mapping exists
    if all(v < 0 for v in result):
        return None

    return result


def _spool_color_to_hex(rgba: str | None) -> str | None:
    """Normalise a ``Spool.rgba`` value (``RRGGBBAA`` hex, no ``#``) to the
    ``#RRGGBB`` form archives store in ``filament_color``.

    Alpha is dropped — the archive colour list and the Color Distribution
    graph treat filament colour as opaque. Returns ``None`` for a missing or
    too-short value so the caller can fall back to the 3MF colour.
    """
    if not rgba:
        return None
    h = rgba.strip().lstrip("#")
    if len(h) < 6:
        return None
    return "#" + h[:6].upper()


def _archive_colors_from_spools(filament_usage: list[dict], results: list[dict]) -> list[str] | None:
    """Slot-ordered, de-duplicated hex colours for an archive's ``filament_color``,
    taken from the inventory spools that actually fed the print (#1494).

    The slicer's 3MF carries its own ``filament_colour`` per slot — a value
    picked independently of the colour the user curates on the matched
    inventory spool. So an archive printed from a ``#000000`` inventory spool
    would otherwise show the slicer's near-black ``#161616``. Once usage
    tracking has resolved the used slots to spools, the spool colours are the
    authoritative source and replace the 3MF values.

    Returns ``None`` — leave the 3MF colour untouched — unless *every* slot
    with non-zero usage was matched to a spool that carries a colour. A
    partial rewrite would silently drop the unmatched slots' colours from the
    archive (and the Color Distribution graph), so it is all-or-nothing.
    """
    used_slots = {u["slot_id"] for u in filament_usage if u.get("used_g", 0) > 0 and u.get("slot_id") is not None}
    if not used_slots:
        return None

    slot_color: dict[int, str] = {}
    for r in results:
        slot_id = r.get("slot_id")
        color = r.get("color")
        if slot_id is not None and color:
            slot_color.setdefault(slot_id, color)

    if not used_slots.issubset(slot_color):
        return None

    ordered: list[str] = []
    for slot_id in sorted(used_slots):
        color = slot_color[slot_id]
        if color not in ordered:
            ordered.append(color)
    return ordered


def _match_slots_by_color(
    filament_usage: list[dict],
    ams_raw: dict | list | None,
) -> list[int] | None:
    """Match 3MF filament slots to AMS trays by color.

    Fallback mapping for printers that don't provide the MQTT mapping field
    or request topic subscription (e.g. A1, A1 Mini, P1S, P2S).

    Compares the 3MF slicer filament color (per slot) against each AMS tray's
    color to find a unique match. Only returns a mapping if every used slot
    matches exactly one tray (no ambiguity).

    Args:
        filament_usage: List of 3MF slot dicts with 'slot_id', 'color', 'type'
        ams_raw: raw_data["ams"] dict or list from printer state

    Returns:
        List of global tray IDs indexed by slicer slot (0-based), or None.
    """
    if not filament_usage or not ams_raw:
        return None

    ams_data = ams_raw.get("ams", []) if isinstance(ams_raw, dict) else ams_raw if isinstance(ams_raw, list) else []
    if not ams_data:
        return None

    # Build map of normalized color → list of global tray IDs
    color_to_trays: dict[str, list[int]] = {}
    for ams_unit in ams_data:
        ams_id = int(ams_unit.get("id", 0))
        for tray in ams_unit.get("tray", []):
            tray_id = int(tray.get("id", 0))
            tray_color = tray.get("tray_color", "")
            tray_type = tray.get("tray_type", "")
            if not tray_color or not tray_type:
                continue
            # Normalize AMS color: strip alpha (last 2 chars), lowercase
            norm = tray_color[:6].lower() if len(tray_color) >= 6 else tray_color.lower()
            if ams_id >= 128:
                global_id = ams_id  # AMS-HT
            else:
                global_id = ams_id * 4 + tray_id
            color_to_trays.setdefault(norm, []).append(global_id)

    if not color_to_trays:
        return None

    # Find max slot_id to size the result array
    max_slot = max(u.get("slot_id", 0) for u in filament_usage)
    if max_slot <= 0:
        return None

    result = [-1] * max_slot
    used_trays: set[int] = set()

    for usage in filament_usage:
        slot_id = usage.get("slot_id", 0)
        if slot_id <= 0:
            continue
        slot_color = usage.get("color", "").lstrip("#").lower()
        if len(slot_color) < 6:
            return None  # Can't match without a valid color

        slot_color = slot_color[:6]  # Strip alpha if present
        candidates = color_to_trays.get(slot_color, [])
        # Filter out trays already claimed by another slot
        available = [t for t in candidates if t not in used_trays]

        if len(available) != 1:
            # Ambiguous (multiple trays with same color) or no match
            return None

        result[slot_id - 1] = available[0]
        used_trays.add(available[0])

    # Only return if at least one valid mapping exists
    if all(v < 0 for v in result):
        return None

    logger.info("[UsageTracker] Color-matched slot_to_tray: %s", result)
    return result


def _global_tray_to_ams_key(global_tray_id: int) -> tuple[int, int]:
    """Convert a global tray id to its ``(ams_id, tray_id)`` assignment key.

    254/255 → external spool (sentinel ams 255); ≥128 → AMS-HT (one slot per
    unit); else a regular 4-slot AMS. Single origin shared by the remain%-delta
    fallback and the 3MF per-segment split so the encoding never drifts.
    """
    if global_tray_id >= 254:
        return (255, global_tray_id - 254)
    if global_tray_id >= 128:
        return (global_tray_id, 0)
    return (global_tray_id // 4, global_tray_id % 4)


def _print_feeder_keys(
    ams_mapping: list[int] | None,
    state=None,
    extra_global_tray: int | None = None,
    *,
    witnesses: Witnesses = "all",
) -> set[tuple[int, int]]:
    """The ``(ams_id, tray_id)`` slots a print actually fed from.

    Three independent witnesses, unioned because each is blind where another sees:
    the dispatch ``ams_mapping`` (the decided slots, durable on the queue item), the
    ``tray_change_log`` (every slot the job switched to mid-print, incl. a firmware
    auto-refill's backup) and one caller-supplied global tray — print-start's
    ``tray_now`` for the remain%-delta fallback, the job's ``last_loaded_tray`` for a
    terminal that has no session left.

    One origin because two consumers ask the same question for opposite reasons: the
    fallback needs it to NOT charge a slot the print never touched (#1269), and the
    zero-gram guard needs it to name the slot that fed a print charging nothing. A
    second copy would let those two disagree about what "this print's feeder" means.

    ``witnesses`` is how they differ, named at the call site rather than implied by a
    null argument:

    * ``"all"`` — mapping ∪ change log ∪ ``extra_global_tray``. The remain%-delta
      fallback wants every candidate, because it only WIDENS a set it then
      intersects against a real remain% drop, so a spurious member costs nothing.
    * ``"dispatch_only"`` — the mapping alone, and the observational arguments are
      ignored outright. The change log and ``tray_now`` observe what is physically
      in the feed path, and a motion-only job — an eject sweep, an empty-bed dry-run
      — inherits the previous print's still-loaded tray (the log is even seeded from
      ``tray_now`` at print start, ``bambu_mqtt`` W6.1). Naming a feeder from those
      would page the zero-gram guard after every eject on every printer. The mapping
      is a DECISION rather than an observation, and an eject decides no AMS slot.
    """
    keys: set[tuple[int, int]] = set()
    for gid in ams_mapping or []:
        if isinstance(gid, int) and gid >= 0:
            keys.add(_global_tray_to_ams_key(gid))
    if witnesses == "dispatch_only":
        return keys
    for change in getattr(state, "tray_change_log", None) or []:
        if isinstance(change, (tuple, list)) and len(change) >= 1:
            gid = change[0]
            if isinstance(gid, int) and gid >= 0:
                keys.add(_global_tray_to_ams_key(gid))
    if isinstance(extra_global_tray, int) and extra_global_tray >= 0:
        keys.add(_global_tray_to_ams_key(extra_global_tray))
    return keys


def _assign_segments_to_slots(
    tray_change_log: list,
    slot_to_tray: list | None,
    nonzero_slot_ids: list[int],
) -> dict[int, list[tuple[int, int]]]:
    """Partition a print's whole-run tray-change log into per-slot feeder segments.

    ``tray_change_log`` is the printer's temporal record of ``tray_now`` switches
    (``[(global_tray_id, layer_num), ...]``) spanning the WHOLE print, so a
    multi-colour print interleaves every colour's segments and an AMS
    auto-refill / backup switch (#957) appends a sibling tray. To split each 3MF
    filament's weight to the tray that actually fed it, the log is first grouped
    per slot:

    * One active filament → every segment fed it (the classic single-filament
      split — mapping-independent and unchanged).
    * Several active filaments WITH a slicer slot→tray mapping → each segment's
      tray is reverse-mapped to its slot; an *orphan* tray (a backup sibling
      mapped to no slot) inherits the immediately-preceding segment's slot,
      because a backup engages the instant the running spool dries — while that
      colour is still extruding, so the tray just before it in the log is the one
      it is standing in for. A leading orphan (no predecessor) is dropped.
    * Several active filaments with NO resolved mapping → unattributable, return
      ``{}`` so the caller charges each slot to its position-default tray, as
      before (never a wrong guess).

    Returns ``{slot_id: [(global_tray_id, start_layer), ...]}`` in layer order;
    a slot whose segment list has <2 distinct trays is left to the normal path.
    """
    segments: list[tuple[int, int]] = []
    for change in tray_change_log or []:
        if isinstance(change, (tuple, list)) and len(change) >= 2 and isinstance(change[0], int):
            layer = change[1] if isinstance(change[1], int) else 0
            segments.append((change[0], layer))
    if not segments:
        return {}

    if len(nonzero_slot_ids) == 1:
        return {nonzero_slot_ids[0]: segments}

    reverse: dict[int, int] = {}
    if slot_to_tray:
        for idx, tray in enumerate(slot_to_tray):
            if isinstance(tray, int) and tray >= 0:
                reverse.setdefault(tray, idx + 1)  # slot_id is 1-based
    if not reverse:
        return {}

    grouped: dict[int, list[tuple[int, int]]] = {}
    prev_slot: int | None = None
    for tray, layer in segments:
        slot = reverse.get(tray, prev_slot)  # orphan/backup inherits the preceding segment
        if slot is None:
            continue  # leading orphan — no colour to attribute it to
        prev_slot = slot
        grouped.setdefault(slot, []).append((tray, layer))

    nonzero = set(nonzero_slot_ids)
    return {sid: segs for sid, segs in grouped.items() if sid in nonzero}


@dataclass
class PrintSession:
    printer_id: int
    print_name: str
    started_at: datetime
    tray_remain_start: dict[tuple[int, int], int] = field(default_factory=dict)
    # tray_now at print start (correct value, unlike at completion where it's 255)
    tray_now_at_start: int = -1
    # Snapshot of spool assignments at print start: {(ams_id, tray_id): spool_id}
    # Prevents usage loss when on_ams_change unlinks a spool mid-print
    spool_assignments: dict[tuple[int, int], int] = field(default_factory=dict)
    # AMS mapping from print command (captured at start, needed when auto-archive is off)
    ams_mapping: list[int] | None = None
    # Queue item's plate_id when this print is a multi-plate 3MF dispatched for a
    # single plate (#1697). None for non-queue prints — the file's first/only plate
    # is the default and the 3MF parser already returns the full file in that case.
    plate_id: int | None = None


# Module-level storage, keyed by printer_id
_active_sessions: dict[int, PrintSession] = {}

# Queue-item statuses a completion-time run-context lookup accepts. The scheduler
# flips the queue item to a terminal status (main.py) BEFORE usage tracking runs,
# so a completion-time lookup must accept the already-stamped terminal states, not
# just "printing" — the single canonical tuple used by both _resolve_run_context
# and the archive-keyed fallback inside _track_from_3mf.
_RUN_CONTEXT_STATUSES = ("printing", "completed", "failed", "cancelled", "aborted")


def _parse_ams_mapping(raw: str | None) -> list[int] | None:
    """Parse a PrintQueueItem.ams_mapping JSON string to a list, or None."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, list) else None


async def _resolve_run_context(
    db: AsyncSession,
    printer_id: int,
    data: dict,
    archive_id: int | None,
    session: "PrintSession | None",
) -> tuple[int | None, list[int] | None]:
    """Resolve ``(plate_id, ams_mapping)`` for a completing print from the most
    durable source available.

    ``plate_id`` used to flow only from the in-memory :class:`PrintSession`, so a
    completion whose session did not survive (server restart mid-print, or a
    ``reconcile_stale_active_prints`` synthesis) resolved ``plate_id=None`` and
    then summed EVERY plate of a multi-plate 3MF into the run — over-charging the
    unit (#usage-integrity). Both durable copies live on the dispatched queue
    item, so the resolution order is:

    1. the live session's captured values (the print-start truth — one path);
    2. the queue item whose ``dispatch_subtask_id`` equals the terminal payload's
       ``subtask_id`` (the id Bambuddy minted for this exact dispatch), any status;
    3. the queue item linked to this ``archive_id`` (accepting any terminal status
       the scheduler may already have stamped — see ``_RUN_CONTEXT_STATUSES``);
    4. the ARCHIVE's own ``plate_id``, stamped at print start from the printer's
       ``gcode_file`` echo.

    ``resolve_active_plate_id`` in farm_correlation is deliberately NOT reused: it
    filters ``status == "printing"``, which is wrong at completion (the row is
    already terminal by the time usage runs).

    **Why tier 4 had to exist.** Tiers 1–3 are all FARM state: a session the farm
    opened, a queue item the farm dispatched. A print the farm did not dispatch —
    a Bambu Studio LAN print, a screen start, a large share of this farm's work —
    matches none of them, so every such completion resolved ``plate_id=None`` and
    ``_track_from_3mf`` then refused to charge a multi-plate file rather than sum
    every plate into one run (24 refusals in the 30 days to 2026-08-23). The
    printer had been stating the plate on every push the whole time. Tier 4 is that
    statement, read once at print start when the printer is still saying it and
    stored on the row, because by completion the echo is long gone.

    Tier 4 answers the PLATE only. The AMS mapping stays farm state — an archive
    row records no dispatch decision — so a foreign print's mapping is still None
    and the observational lanes downstream price it.

    Each tier logs which one answered (``tier=session|queue_item|archive|none``),
    in the style of ``spool_respool._mark_tray_spent``: a plate that resolves from
    the wrong tier is the difference between charging one plate and charging none,
    and the log is where that is diagnosable after the fact.
    """
    # 1. Session fast path — print-start captured both values, no query needed.
    if session is not None:
        if session.plate_id is not None:
            _log_plate_tier(printer_id, archive_id, session.plate_id, "session")
            return session.plate_id, session.ams_mapping
        plate_id = await _archive_plate_id(db, archive_id)
        _log_plate_tier(printer_id, archive_id, plate_id, "archive" if plate_id is not None else "none")
        return plate_id, session.ams_mapping

    item = await _resolve_run_item(db, (data.get("subtask_id") or "").strip() or None, archive_id)
    ams_mapping = _parse_ams_mapping(item.ams_mapping) if item is not None else None
    if item is not None and item.plate_id is not None:
        _log_plate_tier(printer_id, archive_id, item.plate_id, "queue_item")
        return item.plate_id, ams_mapping

    plate_id = await _archive_plate_id(db, archive_id)
    _log_plate_tier(printer_id, archive_id, plate_id, "archive" if plate_id is not None else "none")
    return plate_id, ams_mapping


def _log_plate_tier(printer_id: int, archive_id: int | None, plate_id: int | None, tier: str) -> None:
    """One line naming which tier of :func:`_resolve_run_context` answered."""
    logger.info(
        "[UsageTracker] printer %s archive %s: printed plate %s (tier=%s)",
        printer_id,
        archive_id,
        plate_id if plate_id is not None else "UNKNOWN",
        tier,
    )


async def _archive_plate_id(db: AsyncSession, archive_id: int | None) -> int | None:
    """The plate stamped on this archive row at print start, or None.

    The one durable answer for a print no farm queue item claims. Read as a scalar
    rather than through the ORM row because the caller wants exactly this column
    and may hold no archive instance at all.
    """
    if not archive_id:
        return None
    from backend.app.models.archive import PrintArchive

    result = await db.execute(select(PrintArchive.plate_id).where(PrintArchive.id == archive_id))
    return result.scalar_one_or_none()


async def _resolve_run_item(db: AsyncSession, subtask_id: str | None, archive_id: int | None):
    """The farm queue item a completing print belongs to, or None.

    ONE origin for "which dispatched unit was this print", shared by
    :func:`_resolve_run_context` (which wants its plate + mapping) and the 3MF
    donor fallback in :func:`_track_from_3mf` (which wants its source file). Two
    rows can answer, most-bound first:

    1. the item whose ``dispatch_subtask_id`` equals the terminal payload's
       ``subtask_id`` — the id Bambuddy minted for this exact dispatch, accepted at
       any status because the scheduler may already have stamped the row terminal;
    2. the item linked to this ``archive_id`` (the id-less path: a firmware that
       resets ``subtask_id`` on cancel, or a pre-stamping row). Reprints reuse the
       archive, so the most recently started matching row wins.

    Returns None for every print the farm did not dispatch, which is what keeps the
    foreign/screen-print lanes on their own (guessing) path.
    """
    from backend.app.models.print_queue import PrintQueueItem

    if subtask_id:
        result = await db.execute(
            select(PrintQueueItem)
            .where(PrintQueueItem.dispatch_subtask_id == subtask_id)
            .order_by(PrintQueueItem.started_at.desc())
        )
        item = result.scalars().first()
        if item is not None:
            return item

    if archive_id:
        result = await db.execute(
            select(PrintQueueItem)
            .where(PrintQueueItem.archive_id == archive_id)
            .where(PrintQueueItem.status.in_(_RUN_CONTEXT_STATUSES))
            .order_by(PrintQueueItem.started_at.desc())
        )
        return result.scalars().first()

    return None


def _to_epoch_seconds(value: datetime | None) -> float | None:
    """Convert datetime to epoch seconds, assuming UTC for naive values."""
    if value is None:
        return None
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


async def _resolve_spool_id_for_tray(
    printer_id: int,
    ams_id: int,
    tray_id: int,
    db: AsyncSession,
    spool_assignments_snapshot: dict[tuple[int, int], int] | None = None,
    print_started_at: datetime | None = None,
) -> int | None:
    """Resolve spool ID for a tray with safe support for mid-print reassignment.

    Resolution order:
    1. If snapshot exists and live assignment changed *during this print*, use live spool.
    2. Otherwise use snapshot spool when available.
    3. Fall back to live assignment.
    """
    key = (ams_id, tray_id)
    snapshot_spool_id = spool_assignments_snapshot.get(key) if spool_assignments_snapshot else None

    # Backward-compatible fast path: if we have a snapshot but no print-start
    # timestamp, preserve legacy behavior and avoid extra DB lookups.
    if snapshot_spool_id is not None and print_started_at is None:
        return snapshot_spool_id

    result = await db.execute(
        select(SpoolAssignment).where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    live_assignment = result.scalar_one_or_none()

    if snapshot_spool_id is not None:
        if live_assignment and live_assignment.spool_id != snapshot_spool_id:
            live_created_ts = _to_epoch_seconds(getattr(live_assignment, "created_at", None))
            started_ts = _to_epoch_seconds(print_started_at)
            if live_created_ts is not None and started_ts is not None and live_created_ts >= started_ts:
                logger.info(
                    "[UsageTracker] Assignment changed during print for printer %d AMS%d-T%d: snapshot spool %d -> live spool %d",
                    printer_id,
                    ams_id,
                    tray_id,
                    snapshot_spool_id,
                    live_assignment.spool_id,
                )
                return live_assignment.spool_id
        return snapshot_spool_id

    if live_assignment:
        return live_assignment.spool_id

    return None


async def on_print_start(printer_id: int, data: dict, printer_manager, db: AsyncSession | None = None) -> None:
    """Capture AMS tray remain% and spool assignments at print start."""
    state = printer_manager.get_status(printer_id)
    if not state or not state.raw_data:
        logger.debug("[UsageTracker] No state for printer %d, skipping", printer_id)
        return

    ams_raw = state.raw_data.get("ams", [])
    ams_data = ams_raw.get("ams", []) if isinstance(ams_raw, dict) else ams_raw if isinstance(ams_raw, list) else []

    tray_remain_start: dict[tuple[int, int], int] = {}
    skipped_invalid: list[str] = []

    for ams_unit in ams_data:
        ams_id = int(ams_unit.get("id", 0))
        for tray in ams_unit.get("tray", []):
            tray_id = int(tray.get("id", 0))
            remain = tray.get("remain", -1)
            if isinstance(remain, int) and 0 <= remain <= 100:
                tray_remain_start[(ams_id, tray_id)] = remain
            else:
                skipped_invalid.append(f"AMS{ams_id}-T{tray_id}(remain={remain})")

    # Also capture VT (external) tray remain% — these are separate from AMS units
    vt_tray_raw = state.raw_data.get("vt_tray") or []
    if isinstance(vt_tray_raw, dict):
        vt_tray_raw = [vt_tray_raw]
    for vt in vt_tray_raw:
        if not isinstance(vt, dict):
            continue
        vt_id = int(vt.get("id", 254))
        # VT tray id 254 → (ams_id=255, tray_id=0), id 255 → (ams_id=255, tray_id=1)
        vt_tray_id = vt_id - 254
        remain = vt.get("remain", -1)
        if isinstance(remain, int) and 0 <= remain <= 100:
            tray_remain_start[(255, vt_tray_id)] = remain
        else:
            skipped_invalid.append(f"VT{vt_id}(remain={remain})")

    if skipped_invalid:
        logger.info(
            "[UsageTracker] Skipped trays with invalid remain%% for printer %d: %s",
            printer_id,
            ", ".join(skipped_invalid),
        )

    if not ams_data and not vt_tray_raw:
        logger.debug("[UsageTracker] No AMS or VT tray data for printer %d, skipping", printer_id)
        return

    print_name = data.get("subtask_name", "") or data.get("filename", "unknown")

    # Capture tray_now at print start (reliable, unlike at completion where it's 255)
    tray_now_at_start = state.tray_now if state else -1

    # --- Diagnostic logging: dump mapping-related MQTT fields at print start ---
    # This helps us understand what each printer model reports for slot-to-tray mapping.
    mapping_field = state.raw_data.get("mapping")
    logger.info(
        "[UsageTracker] PRINT START printer %d: mapping=%s, tray_now=%d, last_loaded_tray=%s",
        printer_id,
        mapping_field,
        tray_now_at_start,
        getattr(state, "last_loaded_tray", "N/A"),
    )
    # Log all raw_data keys containing "map" or "ams" for discovery
    map_keys = {k: state.raw_data[k] for k in state.raw_data if "map" in k.lower()}
    if map_keys:
        logger.info("[UsageTracker] PRINT START printer %d: mapping-related keys: %s", printer_id, map_keys)
    # Log per-tray summary: tray_now, tray_tar, tray_type, tray_color for each slot
    for ams_unit in ams_data:
        ams_id = int(ams_unit.get("id", 0))
        tray_summary = []
        for tray in ams_unit.get("tray", []):
            tray_summary.append(
                f"T{tray.get('id', '?')}(type={tray.get('tray_type', '')}, "
                f"color={tray.get('tray_color', '')}, "
                f"now={ams_raw.get('tray_now', '?') if isinstance(ams_raw, dict) else '?'}, "
                f"tar={ams_raw.get('tray_tar', '?') if isinstance(ams_raw, dict) else '?'})"
            )
        logger.info("[UsageTracker] PRINT START printer %d AMS %d: %s", printer_id, ams_id, ", ".join(tray_summary))

    # Snapshot spool assignments so usage isn't lost if on_ams_change unlinks mid-print
    spool_assignments: dict[tuple[int, int], int] = {}
    if db:
        assign_result = await db.execute(select(SpoolAssignment).where(SpoolAssignment.printer_id == printer_id))
        for assignment in assign_result.scalars().all():
            spool_assignments[(assignment.ams_id, assignment.tray_id)] = assignment.spool_id
        if spool_assignments:
            logger.info(
                "[UsageTracker] Snapshotted %d spool assignments for printer %d: %s",
                len(spool_assignments),
                printer_id,
                {f"{k[0]}-{k[1]}": v for k, v in spool_assignments.items()},
            )

    # Capture the queue item's plate_id so 3MF parsing at completion is scoped to
    # the plate that actually ran, not the whole multi-plate file (#1697). Shared
    # with the archive-creation path via the single farm_correlation resolver so
    # id-matching stays consistent (prefers the dispatch_subtask_id-matched item).
    plate_id: int | None = None
    if db:
        from backend.app.services.farm_correlation import resolve_active_plate_id

        plate_id = await resolve_active_plate_id(db, printer_id, data.get("subtask_id"))

    # Always create session (even without valid remain data) so print_name
    # is available at completion for 3MF-based tracking
    session = PrintSession(
        printer_id=printer_id,
        print_name=print_name,
        started_at=datetime.now(timezone.utc),
        tray_remain_start=tray_remain_start,
        tray_now_at_start=tray_now_at_start,
        spool_assignments=spool_assignments,
        ams_mapping=data.get("ams_mapping"),
        plate_id=plate_id,
    )
    _active_sessions[printer_id] = session

    if tray_remain_start:
        logger.info(
            "[UsageTracker] Captured start remain%% for printer %d (%d trays): %s",
            printer_id,
            len(tray_remain_start),
            {f"{k[0]}-{k[1]}": v for k, v in tray_remain_start.items()},
        )
    else:
        logger.debug("[UsageTracker] No valid remain%% for printer %d, 3MF fallback available", printer_id)


async def on_print_complete(
    printer_id: int,
    data: dict,
    printer_manager,
    db: AsyncSession,
    archive_id: int | None = None,
    ams_mapping: list[int] | None = None,
) -> list[dict]:
    """Compute consumption deltas and update spool weight_used/last_used.

    Uses two tracking strategies in priority order:
    1. 3MF per-filament estimates (primary) — precise slicer data for all spools
    2. AMS remain% delta (fallback) — only for trays not already handled by 3MF

    Returns a list of dicts describing what was logged (for WebSocket broadcast).
    """
    from sqlalchemy import select

    from backend.app.api.routes.settings import get_setting
    from backend.app.models.spool_usage_history import SpoolUsageHistory

    session = _active_sessions.pop(printer_id, None)
    status = data.get("status", "completed")
    results = []
    handled_trays: set[tuple[int, int]] = set()

    # Idempotency guard: a completion can be delivered more than once for the same
    # run — the reconcile synthesis racing the real MQTT terminal, or a manual
    # re-finalize — and finalizing twice double-charges the spool. Anchor on the
    # archive's started_at (reset to now on every reprint, main.py): a usage-history
    # row created at/after started_at means THIS run already finalized. Reprints
    # reset started_at, so their older rows fall before it and never block a fresh
    # finalize. The pop above must still happen so the (now-stale) session is cleared.
    if archive_id:
        from sqlalchemy import func

        from backend.app.models.archive import PrintArchive

        started_result = await db.execute(select(PrintArchive.started_at).where(PrintArchive.id == archive_id))
        started_at = started_result.scalar_one_or_none()
        if started_at is not None:
            # started_at is app-written tz-aware UTC; SpoolUsageHistory.created_at is
            # naive UTC (SQLite server_default). Strip tzinfo — compare on naive UTC.
            started_naive = started_at.replace(tzinfo=None) if started_at.tzinfo else started_at
            existing_result = await db.execute(
                select(func.count())
                .select_from(SpoolUsageHistory)
                .where(SpoolUsageHistory.archive_id == archive_id)
                .where(SpoolUsageHistory.created_at >= started_naive)
            )
            existing_count = existing_result.scalar()
            if isinstance(existing_count, int) and existing_count > 0:
                logger.info("[UsageTracker] usage already finalized for archive %s — skipping", archive_id)
                return []

    # Fetch default filament cost from settings for fallback
    default_cost_str = await get_setting(db, "default_filament_cost")
    default_filament_cost = float(default_cost_str) if default_cost_str else 0.0

    # Resolve the printed plate + AMS mapping from the most durable source (live
    # session → dispatch_subtask_id-matched queue item → archive-linked queue item)
    # so multi-plate 3MF usage stays plate-scoped even when the session did not
    # survive to completion (restart mid-print / reconcile synthesis).
    resolved_plate_id, resolved_ams_mapping = await _resolve_run_context(db, printer_id, data, archive_id, session)

    # Prefer the caller-supplied mapping (MQTT request topic / register); fall back
    # to the durably-resolved one (which is the session's when a session exists).
    if not ams_mapping:
        ams_mapping = resolved_ams_mapping

    logger.info(
        "[UsageTracker] on_print_complete: printer=%d, archive=%s, session=%s, ams_mapping=%s",
        printer_id,
        archive_id,
        "yes" if session else "no",
        ams_mapping,
    )

    # --- Diagnostic logging: dump mapping-related MQTT fields at print completion ---
    state = printer_manager.get_status(printer_id)
    if state and state.raw_data:
        logger.info(
            "[UsageTracker] PRINT COMPLETE printer %d: mapping=%s, tray_now=%s, last_loaded_tray=%s",
            printer_id,
            state.raw_data.get("mapping"),
            state.tray_now,
            getattr(state, "last_loaded_tray", "N/A"),
        )

    # --- Path 1 (PRIMARY): 3MF per-filament estimates ---
    print_name = (
        (session.print_name if session else None) or data.get("subtask_name", "") or data.get("filename", "unknown")
    )

    # When auto-archive is disabled (archive_id=None), try to find a 3MF by filename
    # from the library or previous archives so we can still track filament usage.
    threemf_path = None
    if not archive_id:
        from backend.app.core.config import settings as app_settings

        search_filename = data.get("filename") or data.get("subtask_name") or (session.print_name if session else "")
        if search_filename:
            threemf_path = await _find_3mf_by_filename(printer_id, search_filename, db, app_settings.base_dir)

    # The echoed dispatch id lets the 3MF lane fall back to the farm's OWN donor
    # file when neither the archive nor a same-named copy yields one — so it is also
    # a reason to ENTER that lane with no archive at all (auto-archive off): a farm
    # dispatch is chargeable from the file it was dispatched with, always.
    terminal_subtask = (data.get("subtask_id") or "").strip() or None

    if archive_id or threemf_path or terminal_subtask:
        threemf_results = await _track_from_3mf(
            printer_id,
            archive_id,
            status,
            print_name,
            handled_trays,
            printer_manager,
            db,
            ams_mapping=ams_mapping,
            tray_now_at_start=session.tray_now_at_start if session else -1,
            last_progress=data.get("last_progress", 0.0),
            last_layer_num=data.get("last_layer_num", 0),
            default_filament_cost=default_filament_cost,
            spool_assignments=session.spool_assignments if session else None,
            print_started_at=session.started_at if session else None,
            threemf_path=threemf_path,
            plate_id=resolved_plate_id,
            subtask_id=terminal_subtask,
        )
        results.extend(threemf_results)

    # --- Path 2 (FALLBACK): AMS remain% delta (only for trays not handled by 3MF) ---
    if session and session.tray_remain_start:
        state = printer_manager.get_status(printer_id)
        if state and state.raw_data:
            ams_raw = state.raw_data.get("ams", [])
            ams_data = (
                ams_raw.get("ams", []) if isinstance(ams_raw, dict) else ams_raw if isinstance(ams_raw, list) else []
            )

            # Build set of trays actually involved in this print (#1269).
            # Without this guard, swapping a spool in an UNUSED slot mid-print
            # makes that slot's remain% drop to 0, which the fallback below
            # would otherwise charge to the originally-assigned spool.
            print_used_keys = _print_feeder_keys(ams_mapping, state, session.tray_now_at_start)

            # Collect all trays to check: AMS trays + VT (external) trays
            # Each entry: (ams_id_for_assignment, tray_id_for_assignment, current_remain, label)
            trays_to_check: list[tuple[int, int, int, str]] = []

            for ams_unit in ams_data:
                ams_id = int(ams_unit.get("id", 0))
                for tray in ams_unit.get("tray", []):
                    tray_id = int(tray.get("id", 0))
                    remain = tray.get("remain", -1)
                    trays_to_check.append((ams_id, tray_id, remain, f"AMS{ams_id}-T{tray_id}"))

            # VT (external) trays — same remain% delta logic
            vt_tray_raw = state.raw_data.get("vt_tray") or []
            if isinstance(vt_tray_raw, dict):
                vt_tray_raw = [vt_tray_raw]
            for vt in vt_tray_raw:
                if not isinstance(vt, dict):
                    continue
                vt_id = int(vt.get("id", 254))
                vt_tray_id = vt_id - 254  # 254→0, 255→1
                remain = vt.get("remain", -1)
                trays_to_check.append((255, vt_tray_id, remain, f"VT{vt_id}"))

            for assign_ams_id, assign_tray_id, current_remain, tray_label in trays_to_check:
                key = (assign_ams_id, assign_tray_id)

                if key in handled_trays:
                    continue  # Already tracked via 3MF

                if key not in session.tray_remain_start:
                    continue

                # Skip trays the print never touched. Only enforce when we have
                # evidence of which trays the print used; if print_used_keys is
                # empty (no mapping, no change log, no tray_now_at_start) keep
                # the legacy behavior of scanning every tray.
                if print_used_keys and key not in print_used_keys:
                    logger.info(
                        "[UsageTracker] %s: not in print mapping/tray_change_log — skipping fallback for printer %d",
                        tray_label,
                        printer_id,
                    )
                    continue

                if not isinstance(current_remain, int) or current_remain < 0 or current_remain > 100:
                    logger.info(
                        "[UsageTracker] %s: invalid remain%% at completion (%s), skipping fallback for printer %d",
                        tray_label,
                        current_remain,
                        printer_id,
                    )
                    continue

                start_remain = session.tray_remain_start[key]
                delta_pct = start_remain - current_remain

                if delta_pct <= 0:
                    continue  # No consumption or tray was refilled

                spool_id = await _resolve_spool_id_for_tray(
                    printer_id=printer_id,
                    ams_id=assign_ams_id,
                    tray_id=assign_tray_id,
                    db=db,
                    spool_assignments_snapshot=session.spool_assignments,
                    print_started_at=session.started_at,
                )
                if spool_id is None:
                    logger.info(
                        "[UsageTracker] %s: no spool assigned, skipping fallback for printer %d",
                        tray_label,
                        printer_id,
                    )
                    continue

                # Load spool
                spool_result = await db.execute(select(Spool).where(Spool.id == spool_id))
                spool = spool_result.scalar_one_or_none()
                if not spool:
                    continue

                # Compute weight consumed
                weight_grams = (delta_pct / 100.0) * spool.label_weight

                # Update spool
                spool.weight_used = (spool.weight_used or 0) + weight_grams
                spool.last_used = datetime.now(timezone.utc)

                # Calculate cost for this usage
                cost = None
                cost_per_kg = spool.cost_per_kg if spool.cost_per_kg is not None else default_filament_cost
                if cost_per_kg > 0:
                    cost = round((weight_grams / 1000.0) * cost_per_kg, 2)

                # Insert usage history record
                history = SpoolUsageHistory(
                    spool_id=spool.id,
                    printer_id=printer_id,
                    print_name=session.print_name,
                    weight_used=round(weight_grams, 1),
                    percent_used=delta_pct,
                    status=status,
                    cost=cost,
                    archive_id=archive_id,
                )
                db.add(history)

                handled_trays.add(key)
                results.append(
                    {
                        "spool_id": spool.id,
                        "weight_used": round(weight_grams, 1),
                        "percent_used": delta_pct,
                        "ams_id": assign_ams_id,
                        "tray_id": assign_tray_id,
                        "material": spool.material,
                        "cost": cost,
                        # AMS remain%-delta fallback has no 3MF slot — slot_id
                        # stays None so it is excluded from the colour rewrite.
                        "slot_id": None,
                        "color": _spool_color_to_hex(spool.rgba),
                    }
                )

                logger.info(
                    "[UsageTracker] Spool %d consumed %.1fg (%d%%) on printer %d %s (AMS fallback, %s)",
                    spool.id,
                    weight_grams,
                    delta_pct,
                    printer_id,
                    tray_label,
                    status,
                )

    if results:
        await db.commit()

    # --- Update PrintArchive.cost from THIS print session only ---
    #
    # Cover any filament weight that wasn't tracked by an inventory spool with
    # the global default rate (#1344). Without this, a multi-color print where
    # only some AMS trays are mapped to inventory spools would record only the
    # mapped slots' share — e.g. $0.01 for a 110g print when 3 of 4 trays had
    # no spool record. The initial cost set by archive.py (total grams *
    # primary cost_per_kg) is fine on its own, but this block overwrites it,
    # so the overwrite must reconstruct the whole-print cost.

    if archive_id and results:
        from sqlalchemy import func

        from backend.app.models.archive import PrintArchive
        from backend.app.models.print_log import PrintLogEntry

        archive_result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
        archive = archive_result.scalar_one_or_none()
        if archive:
            total_cost = sum(r.get("cost", 0) or 0 for r in results)
            tracked_grams = sum(r.get("weight_used", 0) or 0 for r in results)
            archive_grams = archive.filament_used_grams or 0
            untracked_grams = max(0.0, archive_grams - tracked_grams)
            if untracked_grams > 0 and default_filament_cost > 0:
                total_cost += (untracked_grams / 1000.0) * default_filament_cost
            if total_cost > 0:
                # Only overwrite archive.cost on the first run. Reprint actuals
                # live in PrintLogEntry; the archive card keeps the first run's
                # cost so a failed reprint doesn't visually clobber a successful
                # 100 g/$X print with a 10 g/$X/10 partial (#1378).
                _existing_runs_result = await db.execute(
                    select(func.count(PrintLogEntry.id)).where(PrintLogEntry.archive_id == archive_id)
                )
                _existing_runs = _existing_runs_result.scalar()
                if not _existing_runs:
                    archive.cost = round(total_cost, 2)
                    await db.commit()

    # A COMPLETED print that charged nothing while a TAGLESS roll fed it is not an
    # edge case, it is an impossibility — and one that costs nothing to produce,
    # because every failure in this module's primary path (no archive, no 3MF, an
    # unreadable plate) returns an EMPTY list rather than raising. Make it loud.
    charged_grams = sum(r.get("weight_used") or 0 for r in results)
    if charged_grams <= 0:
        try:
            await _warn_zero_gram_tagless_charge(
                db,
                printer_id=printer_id,
                archive_id=archive_id,
                status=status,
                print_name=print_name,
                ams_mapping=ams_mapping,
                subtask_id=terminal_subtask,
                state=state,
                last_loaded_tray=getattr(state, "last_loaded_tray", None),
            )
        except Exception:  # noqa: BLE001 — a guard that breaks the thing it guards is worse than the silence
            logger.exception("[UsageTracker] zero-gram check failed for printer %s archive %s", printer_id, archive_id)

    return results


async def _warn_zero_gram_tagless_charge(
    db: AsyncSession,
    *,
    printer_id: int,
    archive_id: int | None,
    status: str,
    print_name: str,
    ams_mapping: list[int] | None,
    subtask_id: str | None = None,
    state=None,
    last_loaded_tray: int | None = None,
) -> None:
    """WARN + notify when a completed print charged 0 g on a tagless feeder (C6).

    **Why this cannot be left to fail loudly on its own.** For a TAGGED roll two
    independent gram sources exist — the slicer 3MF and the AMS remain%-delta — so
    losing one still charges the print. A tagless tray has exactly one: this fleet's
    AMS answers ``remain: -1`` for every untagged roll, always, so the delta lane
    cannot price it and the 3MF is the whole accounting. Every way of losing that
    3MF ends in ``return []`` here, which is indistinguishable from "this print used
    no filament" — a completed print silently charging nothing, forever. That is how
    15 consecutive prints from 2026-08-18 00:12 charged zero grams while two 1 kg
    rolls sat reading "0 g used" after 5.8 h each. Doctrine rule 4 makes tagless gram
    tracking mandatory, so the no-op is a defect and has to announce itself.

    Deliberately narrow, because a false page here trains an operator to ignore a
    true one:

    * **COMPLETED only.** A cancelled or failed print legitimately charges ~nothing
      when it died early; only a finished one makes zero grams impossible.
    * **AMS feeders only** (``ams_id != 255``). An external holder is excluded for
      the same reason its runouts never stamp spent (scenario T14): the left/right
      vt-tray attribution convention is unconfirmed.
    * **Which witnesses may name the feeder depends on whether the FARM DISPATCHED
      this print.** The dispatch ``ams_mapping`` is a DECISION and a motion-only job
      (an eject sweep, an empty-bed dry-run) decides no AMS slot at all, which is
      what makes it the safe default: the observational witnesses
      (``tray_change_log``, ``last_loaded_tray``) report what is physically in the
      feed path, and a motion-only job runs with the PREVIOUS print's roll still
      loaded, so naming a feeder from those would page for a job whose zero grams
      are correct. Production shows that guard doing its job
      (``'eject_production_item890' … (mapping=None)``).

      But a print STARTED FROM THE SCREEN or from Bambu Studio decides no mapping
      either, and its zero grams are the loss this event exists for — so
      ``dispatch_only`` silenced the entire population it was written to catch. The
      widening therefore needs a discriminator that separates "no mapping because
      nothing fed" from "no mapping because the farm never decided one", and it is
      **attribution**: the observational witnesses are admitted only for an archive
      that still has no slicer data (``extra_data["no_3mf_available"]``) AND that
      ``_resolve_run_item`` cannot tie to any dispatched queue unit.

      "Is it an eject" would be the WRONG test, and dangerously close to right. An
      eject genuinely never archives — ``on_print_start`` returns before the archive
      body for any pending-eject or eject-named start — but a **dry-run does**:
      production ``desktop-g6cgc9k`` carries archives 733/734 on printer 2,
      2026-08-18, ``print_name='DRY-RUN single-pass-dwell-jitter'``, 6.4 MB of
      captured 3MF. Only its successful FTPS fetch keeps it out of the flagged
      population, so a single transient failure would hand a motion-only job the
      observational witnesses and page for the previous print's roll. Attribution
      excludes it for the right reason — a dry-run is farm-dispatched and owns a
      queue item — and excludes every future farm-dispatched motion job nobody has
      thought of yet, while a screen start or a Studio print has no item by
      definition.

      The flag stays TRUTHFUL over the print's life: ``ArchiveService`` pops
      ``no_3mf_available`` from ``extra_data`` the moment a late retry attaches the
      captured 3MF (``archive.attach_3mf_to_archive``), so a print that was rescued
      no longer matches and falls back to ``dispatch_only``. A rescued print also
      charges its grams and never reaches here at all.

      Cause-based throughout: nothing here reads the print's size, duration or
      name.
    * **A tagless feeder must actually be identified.** If no AMS slot resolves to a
      tagless spool, the zero is either explainable or unattributable, and an
      unattributable page names nothing an operator can act on — so it stays an INFO
      record instead.

    **What the two surfaces each carry.** The WARN names EVERY zero-charged tagless
    feeder of this print (a multi-material run can lose several rolls' grams at
    once, and a log naming only the first hides the rest of the shortfall). The
    PAGE is deduped ``printer:{id}`` for 6 h and names the FIRST such feeder only:
    its body template is singular by construction ("… but {slot} is fed by tagless
    spool #{spool_id} ({spool_material})"), templates are seeded once and never
    re-seeded on an existing install, so a plural caller would render broken copy on
    every farm already in production. The WARN is the complete record; the page is
    the pointer to it.

    Reports rather than repairs: the grams of a print with no slicer data are not
    recoverable afterwards, and writing an invented figure would replace a visible
    hole with an invisible wrong number (the same reasoning that keeps
    ``detect_spent_contradictions`` non-mutating).
    """
    if status != "completed":
        return

    from backend.app.models.printer import Printer
    from backend.app.services import notify_dedup
    from backend.app.services.notification_service import notification_service
    from backend.app.services.spool_recovery import runout_slot_desc
    from backend.app.services.spool_respool import encode_global_tray
    from backend.app.services.spool_tagless import is_tagless_spool

    # ``runout_slot_desc`` is the ONE origin for slot wording, so this page names a
    # slot the way every other operator surface does. The roll is named by id +
    # material rather than through either module's private ``_spool_label``: this
    # event only ever fires for a TAGLESS row, whose brand and colour_name are blank
    # by construction (auto-minted from ``tagless_default_filament``), so the label
    # helper would degrade to the material anyway — and there are already two copies
    # of it in the tree without this adding a third.

    # ATTRIBUTION decides the witnesses: a print with no slicer data that no
    # dispatched unit claims is a screen/Studio start, whose only feeder witnesses
    # are observational. Anything the farm DISPATCHED keeps ``dispatch_only`` —
    # including a dry-run, which unlike an eject really does archive (prod archives
    # 733/734) and would otherwise false-page on the previous print's roll the first
    # time its 3MF fetch failed. See the docstring.
    lost_its_3mf = await _archive_lost_its_3mf(db, archive_id)
    undispatched = lost_its_3mf and (await _resolve_run_item(db, subtask_id, archive_id)) is None
    witnesses: Witnesses = "all" if undispatched else "dispatch_only"
    feeder_keys = _print_feeder_keys(ams_mapping, state, last_loaded_tray, witnesses=witnesses)
    ams_keys = sorted(key for key in feeder_keys if key[0] != 255)
    if not ams_keys:
        logger.info(
            "[UsageTracker] printer %s archive %s: '%s' completed charging 0 g and no AMS feeder could be "
            "named (mapping=%s, witnesses=%s) — nothing to attribute the shortfall to",
            printer_id,
            archive_id,
            print_name,
            ams_mapping,
            witnesses,
        )
        return

    # Collect EVERY tagless feeder first — a multi-material print can lose several
    # rolls' grams in one completion, and reporting only the first understates the
    # shortfall in the one record that survives (the log).
    tagless_feeders: list[tuple[str, Spool]] = []
    for ams_id, tray_id in ams_keys:
        spool_id = await _resolve_spool_id_for_tray(printer_id, ams_id, tray_id, db)
        if spool_id is None:
            continue
        spool = await db.get(Spool, spool_id)
        if spool is None or not is_tagless_spool(spool):
            continue
        # Through the fork's ONE global-tray codec (invariant 1): the bare ``ams_id * 4``
        # arithmetic that used to sit here is wrong for an AMS-HT unit (``global == ams_id``)
        # and for the external holder (254/255), and both would land on some OTHER unit's
        # slot wording. ``None`` from the encoder falls through to the raw ``AMS%d-T%d``
        # rendering, exactly as an unnameable global already did.
        slot_desc = runout_slot_desc(encode_global_tray(ams_id, tray_id)) or f"AMS{ams_id}-T{tray_id}"
        tagless_feeders.append((slot_desc, spool))

    if not tagless_feeders:
        logger.info(
            "[UsageTracker] printer %s archive %s: '%s' completed charging 0 g; feeders %s are all tagged or "
            "unassigned — a tagged roll has the AMS remain%%-delta as a second gram source, so this zero is not "
            "the impossibility the page reports",
            printer_id,
            archive_id,
            print_name,
            ", ".join(f"AMS{ams_id}-T{tray_id}" for ams_id, tray_id in ams_keys),
        )
        return

    logger.warning(
        "[UsageTracker] ZERO-GRAM CHARGE: printer %s archive %s completed '%s' and charged 0 g, but %s — a "
        "tagless tray reports remain: -1, so the print's 3MF was its only gram source and the archive has none. "
        "Those rolls' ledgers are now short by this print.",
        printer_id,
        archive_id,
        print_name,
        "; ".join(f"{slot_desc} is fed by TAGLESS spool {spool.id}" for slot_desc, spool in tagless_feeders),
    )

    printer = await db.get(Printer, printer_id)
    printer_name = (printer.name if printer is not None else None) or f"Printer {printer_id}"
    # One page per printer per window: this class of bleed repeats on EVERY print,
    # and fifteen identical pages bury the fact they are reporting. The page names
    # the FIRST tagless feeder — its template is singular and is seeded once per
    # install, so plural copy here would render broken on every existing farm; the
    # WARN above is the complete record.
    key = f"printer:{printer_id}"
    last = await notify_dedup.last_sent_at(db, _ZERO_CHARGE_SCOPE, key)
    if last is not None and (datetime.utcnow() - last).total_seconds() < _ZERO_CHARGE_RENOTIFY_S:
        return
    first_slot_desc, first_spool = tagless_feeders[0]
    await notification_service.on_zero_gram_charge(
        printer_id,
        printer_name,
        print_name,
        first_slot_desc,
        first_spool.id,
        (first_spool.material or "unknown material"),
        db,
    )
    await notify_dedup.record_sent(db, _ZERO_CHARGE_SCOPE, key)


async def _archive_lost_its_3mf(db: AsyncSession, archive_id: int | None) -> bool:
    """Whether this archive is a print whose source 3MF was never captured.

    ``main.on_print_start`` writes ``extra_data["no_3mf_available"]`` on the
    fallback archive it creates when the FTPS capture found nothing, and
    ``ArchiveService.attach_3mf_to_archive`` POPS that key the moment a late retry
    lands the file — so the flag reads "this print still has no slicer data", not
    "it once didn't". No archive id means no archive row: an eject sweep, which
    ``on_print_start`` returns from before archiving anything.
    """
    if not archive_id:
        return False
    from backend.app.models.archive import PrintArchive

    result = await db.execute(select(PrintArchive.extra_data).where(PrintArchive.id == archive_id))
    extra = result.scalar_one_or_none()
    return bool(isinstance(extra, dict) and extra.get("no_3mf_available"))


async def _dispatch_donor_for_completion(db: AsyncSession, subtask_id: str | None, archive_id: int | None):
    """The on-disk source 3MF the farm dispatched for this completing print, or None.

    A thin join of the two existing origins — :func:`_resolve_run_item` (which unit)
    and ``farm_correlation.resolve_item_donor`` (which file that unit printed from,
    the same answer the eject builder and the print-start archive capture consume).
    No third resolver: everything about "which file" stays in ``farm_correlation``,
    and the donor's ``local_path`` is BORROWED — read only, never deleted or
    truncated (the 2026-08-15 durable-file-loss class).

    Deliberately WIDER than ``resolve_dispatch_donor``'s id-confirmed-only rule, and
    only because completion is a different question from print start. At START the
    risk is attributing the farm's file to a job that is not ours, which would
    archive somebody else's print as having run our 3MF — so that lane demands
    ``dispatch_subtask_id`` equality. Here the item is reached the same way this
    module already resolves the run's plate and mapping (id first, then the archive
    link this very completion is finalizing), and being wrong costs a gram figure on
    a print that would otherwise have been charged NOTHING — the strictly worse
    error, and the one that is invisible.
    """
    from backend.app.services import farm_correlation

    item = await _resolve_run_item(db, subtask_id, archive_id)
    if item is None:
        return None
    return await farm_correlation.resolve_item_donor(db, item)


async def _3mf_by_print_identity(
    db: AsyncSession,
    base_dir,
    *,
    search_key: str,
    printer_id: int | None,
    exclude_archive_id: int | None,
    context: str,
):
    """The on-disk 3MF for a print identified by ``search_key``, or None.

    ONE implementation for the two lanes that ask this — :func:`_resolve_3mf_fallback`
    (an archive exists but holds no file) and :func:`_find_3mf_by_filename`
    (auto-archive is off, so there is no archive at all). They are the same question
    asked from two states, and a second copy would let them disagree about which
    file a roll is charged from.

    **Identity is adjudicated in PYTHON, on the rows the query returns.** These lanes
    used to express it as ``ilike(f"%{search_base}.%")``, which is wrong twice over:

    * ``LIKE`` treats ``_`` as a single-character WILDCARD, and this farm's names are
      mostly underscores — ``print_identity_key`` even folds every space to one — so
      the "identity" key silently became a fuzzy pattern at the exact point that
      decides which file's grams a roll is charged. A pattern also inherits whatever
      an operator typed into a filename, which is not a thing to hand to SQL.
    * It could not match this corpus anyway. ``print_identity_key`` strips the
      mid-stem ``.gcode`` token; the STORED name keeps it
      (``…PCO-M12-2525.gcode_L1-90_spliced.3mf``), so a pattern with the token
      removed cannot match a path that still has it, wildcards or not. Both lanes
      missed every spliced file — and this is the lane that rescues a foreign print
      whose own 3MF is already deleted from the printer, which is exactly the case
      the print-start plate fix cannot help with.

    So the SQL keeps only predicates that cannot exclude a true match (``.3mf``,
    not-trashed, a real ``file_path``, the printer, the row itself) and orders
    newest-first; the name comparison happens on the returned rows, first hit wins.
    Deliberately NO ``LIMIT``: a limit applied before the comparison makes
    non-matching rows invisible and can step over the true answer — the query-shape
    ruling of 2026-08-20, from the shape-32 resurrection, where an eligibility
    filter ahead of ``LIMIT 1`` silently answered with the newest row that passed.
    The scan is small and bounded by the estate: production carries 46 library rows
    and 786 archives.

    Library rows are matched on the ORIGINAL ``filename`` as well as the storage
    ``file_path``, because a library file may be stored under a generated name that
    could never equal a print key. Archive rows are matched on ``filename`` alone —
    the one field the previous SQL consulted — so this stays strictly TIGHTER than
    what it replaces rather than reaching for new ways to match.
    """
    from pathlib import Path

    from backend.app.models.archive import PrintArchive
    from backend.app.models.library import LibraryFile
    from backend.app.utils.filename import print_identity_key

    def _identifies(*names: str | None) -> bool:
        # print_identity_key takes the basename itself, so a full storage path is a
        # valid argument here. The isinstance check is not decoration: that function
        # raises TypeError on a non-string by design, and this comparison now runs
        # per ROW inside the try — so one row carrying a non-string name would abort
        # the whole lookup and silently degrade to "no file found", which is exactly
        # the invisible-hole failure this module keeps having to close. A name that
        # is not a string simply does not identify anything.
        return any(isinstance(name, str) and name and print_identity_key(name) == search_key for name in names)

    # 1. Library files — a human-added copy of the same print.
    try:
        lib_result = await db.execute(
            LibraryFile.active().where(LibraryFile.file_path.ilike("%.3mf")).order_by(LibraryFile.created_at.desc())
        )
        for lib_file in lib_result.scalars().all():
            if not _identifies(lib_file.filename, lib_file.file_path):
                continue
            lib_path = Path(lib_file.file_path)
            candidate = lib_path if lib_path.is_absolute() else base_dir / lib_file.file_path
            if candidate.exists() and candidate.suffix == ".3mf":
                logger.info("[UsageTracker] %s: found library file %s for '%s'", context, candidate, search_key)
                return candidate
    except Exception as e:  # noqa: BLE001 — a lookup failure just means "no file this way"
        logger.debug("[UsageTracker] %s: library lookup failed: %s", context, e)

    # 2. A previous archive of the same print that kept its own copy.
    try:
        stmt = (
            select(PrintArchive)
            .where(PrintArchive.file_path != "")
            .where(PrintArchive.file_path.isnot(None))
            .order_by(PrintArchive.created_at.desc())
        )
        if printer_id is not None:
            stmt = stmt.where(PrintArchive.printer_id == printer_id)
        if exclude_archive_id is not None:
            stmt = stmt.where(PrintArchive.id != exclude_archive_id)
        prev_result = await db.execute(stmt)
        for prev_archive in prev_result.scalars().all():
            if not _identifies(prev_archive.filename):
                continue
            candidate = base_dir / prev_archive.file_path
            if candidate.exists() and candidate.suffix == ".3mf":
                logger.info(
                    "[UsageTracker] %s: found previous archive %s file for '%s'",
                    context,
                    prev_archive.id,
                    search_key,
                )
                return candidate
    except Exception as e:  # noqa: BLE001 — a lookup failure just means "no file this way"
        logger.debug("[UsageTracker] %s: previous archive lookup failed: %s", context, e)

    return None


async def _resolve_3mf_fallback(archive, db: AsyncSession, base_dir):
    """Try to find a 3MF file from library or a previous archive when the current archive has none.

    This handles fallback archives (FTP download failed) where the 3MF may already exist
    locally from a library upload or a previous successful print of the same file.
    """
    # The print's identity through the fork's ONE "is this the same print?" key. The
    # private replace-chain that stood here was one of three hand-rolled copies of
    # that normalisation — the others being :func:`_find_3mf_by_filename` and
    # ``foreign_archive``'s directory search, whose copy normalised its two sides
    # DIFFERENTLY and so could not match this farm's corpus at all.
    from backend.app.utils.filename import print_identity_key

    search_name = archive.filename or archive.print_name
    if not search_name:
        return None
    search_key = print_identity_key(search_name)
    if not search_key:
        return None

    return await _3mf_by_print_identity(
        db,
        base_dir,
        search_key=search_key,
        printer_id=archive.printer_id,
        exclude_archive_id=archive.id,
        context="3MF fallback",
    )


async def _find_3mf_by_filename(
    printer_id: int,
    filename: str,
    db: AsyncSession,
    base_dir,
):
    """Find a 3MF file by filename from library or previous archives.

    Used when auto-archive is disabled and there's no archive_id, but we still
    need the 3MF slicer data for filament usage tracking.
    """
    # Same one normaliser and the same resolver as :func:`_resolve_3mf_fallback` —
    # this is that lane's auto-archive-off twin and must not key a print's identity
    # differently from it.
    from backend.app.utils.filename import print_identity_key

    search_key = print_identity_key(filename)
    if not search_key:
        return None

    return await _3mf_by_print_identity(
        db,
        base_dir,
        search_key=search_key,
        printer_id=printer_id,
        exclude_archive_id=None,
        context="3MF (no-archive)",
    )


async def _track_from_3mf(
    printer_id: int,
    archive_id: int | None,
    status: str,
    print_name: str,
    handled_trays: set[tuple[int, int]],
    printer_manager,
    db: AsyncSession,
    ams_mapping: list[int] | None = None,
    tray_now_at_start: int = -1,
    last_progress: float = 0.0,
    last_layer_num: int = 0,
    default_filament_cost: float = 0.0,
    spool_assignments: dict[tuple[int, int], int] | None = None,
    print_started_at: datetime | None = None,
    threemf_path=None,
    plate_id: int | None = None,
    subtask_id: str | None = None,
) -> list[dict]:
    """Track usage from 3MF per-filament slicer data (primary path).

    Uses slicer-estimated filament weight for all spools (BL and non-BL).
    For partial prints (failed/aborted), tries per-layer gcode data first,
    then falls back to linear scaling by progress.

    When archive_id is None (auto-archive disabled), a pre-resolved threemf_path
    can be provided to still track filament usage from slicer data.

    File resolution order — ONE lane, three ways of naming its ``file_path``:
    a caller-supplied ``threemf_path`` → the archive's own copy → a same-named
    library/previous-archive file (``_resolve_3mf_fallback``) → the DISPATCH DONOR
    of the farm unit that printed it (``subtask_id``/``archive_id`` →
    ``farm_correlation.resolve_item_donor``). Everything after the path is
    resolved — plate scoping, the per-feeder split, the gram charging — is common
    to all four; only the file's provenance differs.

    When ``plate_id`` is set (queue prints of a single plate from a multi-plate
    3MF), only that plate's filaments contribute. Without it the 3MF parser sums
    every plate, which is correct for direct/library Print flows that always
    target the first or only plate (#1697).

    Slot-to-tray mapping priority:
    1. Stored ams_mapping from print command (reprints/direct prints)
    2. MQTT mapping field from printer state (universal, all print sources)
    3. Queue item ams_mapping (for queue-initiated prints)
    4. tray_now from printer state (for single-filament non-queue prints)
    5. Position-based default using sorted available tray IDs (handles external spools)
    6. Default mapping: slot_id - 1 = global_tray_id (last resort)
    """
    from pathlib import Path

    from backend.app.core.config import settings as app_settings
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.utils.threemf_tools import count_plates_in_slice_info, extract_filament_usage_from_3mf

    file_path: Path | None = threemf_path
    archive: PrintArchive | None = None
    # Which of the four sources produced the gram data, logged once below. These
    # tiers answered SILENTLY until now: a print charged from a previous archive's
    # same-named copy and one charged from its own 3MF are indistinguishable in the
    # log, and they are not equally trustworthy — the fallback tier matches on a
    # NAME, so it can hand back a different slicing of the same project.
    file_tier = "caller" if file_path is not None else "none"

    if file_path is None and archive_id:
        result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
        archive = result.scalar_one_or_none()
        if not archive:
            logger.info("[UsageTracker] 3MF: archive %s not found, skipping", archive_id)
            return []

        # Try archive's own file_path first
        if archive.file_path:
            candidate = app_settings.base_dir / archive.file_path
            if candidate.exists():
                file_path = candidate
                file_tier = "archive_file"

        # Fallback: find 3MF from library or a previous archive with the same filename
        if file_path is None:
            file_path = await _resolve_3mf_fallback(archive, db, app_settings.base_dir)
            if file_path is not None:
                file_tier = "name_fallback"

    # Last resort: the DISPATCH DONOR — the file the farm itself uploaded for this
    # unit. The print-start capture (``farm_correlation.resolve_dispatch_donor`` →
    # ``foreign_archive.locate_3mf_for_print``) normally attaches it to the archive
    # row, but that runs exactly once, at START, and anything that loses it there
    # (an FTPS outage, a fallback archive written before the donor lane existed —
    # printer 1 / archive 783 / spool 338 on 2026-08-20) leaves a COMPLETED print
    # with no gram source at all. The queue item still names the source row and the
    # plate, so completion can ask the same shared origin the eject builder and the
    # archive capture ask, and charge from the borrowed library file directly.
    # A print the farm did not dispatch resolves no item, hence no donor, and keeps
    # the existing skip.
    if file_path is None:
        donor = await _dispatch_donor_for_completion(db, subtask_id, archive_id)
        if donor is not None:
            file_path = donor.local_path
            file_tier = "dispatch_donor"
            if plate_id is None:
                plate_id = donor.plate_id
            logger.info(
                "[UsageTracker] 3MF: charged from the dispatch donor %s (queue item %s, plate %s); the archive "
                "%s carried no 3MF",
                donor.local_path,
                donor.item_id,
                plate_id,
                archive_id,
            )

    if file_path is None:
        logger.info("[UsageTracker] 3MF: no file available for archive %s, skipping", archive_id)
        return []

    logger.info(
        "[UsageTracker] 3MF: printer %s archive %s charging from %s (tier=%s), plate %s",
        printer_id,
        archive_id,
        file_path,
        file_tier,
        plate_id if plate_id is not None else "UNKNOWN",
    )

    # Unknown printed plate on a multi-plate 3MF: with no plate_id the extractor
    # sums EVERY plate, charging the whole file to this one run. Skip 3MF tracking
    # (the remain%-delta fallback in on_print_complete still applies) rather than
    # over-charge. A single-plate file — or an unreadable one (count 0) — proceeds:
    # the extractor returns the sole plate.
    if plate_id is None:
        plate_count = count_plates_in_slice_info(file_path)
        if plate_count > 1:
            logger.warning(
                "[UsageTracker] 3MF: %s has %d plates but the printed plate is unknown "
                "(no session, no queue item) on printer %d — skipping 3MF usage to avoid "
                "charging the whole file",
                file_path,
                plate_count,
                printer_id,
            )
            return []

    # VALIDITY, not trust: the plate must be one this FILE declares.
    #
    # The archive tier of ``_resolve_run_context`` reads a plate parsed from the
    # printer's ``gcode_file`` echo, and that echo has a known degenerate shape — a
    # manual screen RESTART echoes ``subtask_name="project_file"`` with only
    # ``/data/Metadata/plate_N.gcode`` — so it can name a plate belonging to some
    # other print. The file tiers above are name-matched too, so any of them can
    # hand back a slicing that never held this plate. Checking containment costs
    # nothing: B1's index set is already how the print-start capture validates the
    # same file, and the answer is read off the same ``slice_info`` block.
    #
    # A miss REFUSES rather than falling back to a plate-less charge. That is the
    # behaviour this already had — ``extract_filament_usage_from_3mf`` returns []
    # for a plate the file does not declare — made explicit and diagnosable;
    # downgrading to None instead would let a single-plate file be charged as if it
    # were the plate that is missing, which is a wrong number where there is
    # currently an honest zero. An EMPTY set is "unreadable", never "absent", and
    # proceeds untouched.
    if plate_id is not None:
        from backend.app.services.archive import plate_indices_in_3mf

        declared_plates = plate_indices_in_3mf(file_path)
        if declared_plates and plate_id not in declared_plates:
            logger.warning(
                "[UsageTracker] 3MF: printer %s archive %s resolved plate %s but %s declares plates %s "
                "(tier=%s) — refusing to charge from a file that never held this plate",
                printer_id,
                archive_id,
                plate_id,
                file_path,
                sorted(declared_plates),
                file_tier,
            )
            return []

    filament_usage = extract_filament_usage_from_3mf(file_path, plate_id)
    if not filament_usage:
        logger.info("[UsageTracker] 3MF: no filament usage data in %s", file_path)
        return []

    logger.info("[UsageTracker] 3MF: archive %s, plate_id=%s, filament_usage=%s", archive_id, plate_id, filament_usage)

    # --- Resolve slot-to-tray mapping ---
    mapping_source = None

    # 1. Use stored ams_mapping from the print command (reprints/direct prints)
    slot_to_tray = ams_mapping
    if slot_to_tray:
        mapping_source = "print_cmd"

    # 2. Try MQTT mapping field from printer state (universal, all print sources)
    if not slot_to_tray:
        state = printer_manager.get_status(printer_id)
        raw_data = getattr(state, "raw_data", None) if state else None
        if raw_data:
            mqtt_mapping = raw_data.get("mapping")
            decoded = _decode_mqtt_mapping(mqtt_mapping)
            if decoded:
                slot_to_tray = decoded
                mapping_source = "mqtt"

    # 3. Try queue item ams_mapping (queue-initiated prints store the exact mapping)
    if not slot_to_tray and archive_id:
        queue_result = await db.execute(
            select(PrintQueueItem)
            .where(PrintQueueItem.archive_id == archive_id)
            .where(PrintQueueItem.status.in_(_RUN_CONTEXT_STATUSES))
        )
        queue_item = queue_result.scalar_one_or_none()
        if queue_item and queue_item.ams_mapping:
            try:
                slot_to_tray = json.loads(queue_item.ams_mapping)
                mapping_source = "queue"
            except (json.JSONDecodeError, TypeError):
                pass

    # 4. Color-match 3MF filament slots to AMS trays (for printers without mapping field)
    if not slot_to_tray:
        state = printer_manager.get_status(printer_id)
        raw_data = getattr(state, "raw_data", None) if state else None
        if raw_data:
            matched = _match_slots_by_color(filament_usage, raw_data.get("ams"))
            if matched:
                slot_to_tray = matched
                mapping_source = "color_match"

    logger.info(
        "[UsageTracker] 3MF: slot_to_tray=%s (source: %s)",
        slot_to_tray,
        mapping_source or "none",
    )

    # 5. Resolve mid-print tray attribution.
    #    The printer's tray_change_log (temporal record of tray_now switches)
    #    drives a per-segment weight SPLIT whenever a filament fed from >1 tray —
    #    AMS auto-refill / backup when a spool runs dry (#957). It is consulted
    #    for ALL slot counts, not only single-filament prints: a multi-colour run
    #    whose primary colour backup-switched mid-print must still credit the
    #    sibling tray rather than dumping its whole share on the print-start
    #    mapped slot (#006). Segments are grouped per slot first (a colour's own
    #    home tray + the backups that stood in for it); a lone single-filament
    #    print with no resolved mapping and no split falls back to live tray_now.
    nonzero_slots = [u for u in filament_usage if u.get("used_g", 0) > 0]
    nonzero_slot_ids = [u.get("slot_id", 0) for u in nonzero_slots]
    tray_now_override: int | None = None

    split_state = printer_manager.get_status(printer_id)
    _raw_log = getattr(split_state, "tray_change_log", None)
    tray_changes: list[tuple[int, int]] = list(_raw_log) if isinstance(_raw_log, (list, tuple)) else []
    _raw_total = getattr(split_state, "total_layers", 0)
    split_total_layers = _raw_total if isinstance(_raw_total, int) else 0

    slot_segments = _assign_segments_to_slots(tray_changes, slot_to_tray, nonzero_slot_ids)
    any_split = any(len({t for t, _ in (slot_segments.get(sid) or [])}) > 1 for sid in nonzero_slot_ids)

    # `state` keeps its original single-filament meaning for the tray_now
    # fallback below and downstream partial-scaling references.
    state = split_state if len(nonzero_slots) == 1 else None

    if any_split:
        logger.info(
            "[UsageTracker] 3MF: tray_change_log=%s -> per-slot segments=%s (will split weight)",
            tray_changes,
            slot_segments,
        )
    elif not slot_to_tray and len(nonzero_slots) == 1:
        if 0 <= tray_now_at_start <= 254:
            tray_now_override = tray_now_at_start
            logger.info("[UsageTracker] 3MF: using tray_now_at_start=%d (single-filament fallback)", tray_now_at_start)
        elif state and 0 <= state.tray_now <= 254:
            tray_now_override = state.tray_now
            logger.info("[UsageTracker] 3MF: using current tray_now=%d", state.tray_now)
        elif state and 0 <= state.last_loaded_tray <= 253:
            tray_now_override = state.last_loaded_tray
            logger.info("[UsageTracker] 3MF: using last_loaded_tray=%d (post-retract fallback)", state.last_loaded_tray)
        elif state and state.tray_now == 255:
            # 255 = "no filament" on legacy printers, but valid 2nd external spool on H2-series
            vt_tray = state.raw_data.get("vt_tray") or []
            if any(int(vt.get("id", 0)) == 255 for vt in vt_tray if isinstance(vt, dict)):
                tray_now_override = state.tray_now
                logger.info("[UsageTracker] 3MF: using tray_now=255 (H2-series external spool)")
        if tray_now_override is None:
            logger.info(
                "[UsageTracker] 3MF: no valid tray_now (at_start=%d, current=%s, last_loaded=%s)",
                tray_now_at_start,
                state.tray_now if state else "N/A",
                state.last_loaded_tray if state else "N/A",
            )

    # Scale factor for partial prints (failed/aborted)
    if status == "completed":
        scale = 1.0
    else:
        state = printer_manager.get_status(printer_id)
        progress = state.progress if state else 0
        # Firmware resets progress to 0 on cancel — use last valid progress captured during print
        if progress <= 0 and last_progress > 0:
            progress = last_progress
            logger.info("[UsageTracker] 3MF: using last_progress=%.1f (firmware reset current to 0)", last_progress)
        scale = max(0.0, min(progress / 100.0, 1.0))

    # Per-layer gcode accuracy for partial prints
    layer_grams: dict[int, float] | None = None
    if status != "completed":
        state = printer_manager.get_status(printer_id)
        current_layer = state.layer_num if state else 0
        # Firmware resets layer_num to 0 on cancel — use last valid layer captured during print
        if current_layer <= 0 and last_layer_num > 0:
            current_layer = last_layer_num
            logger.info("[UsageTracker] 3MF: using last_layer_num=%d (firmware reset current to 0)", last_layer_num)
        if current_layer > 0:
            try:
                from backend.app.utils.threemf_tools import (
                    extract_filament_properties_from_3mf,
                    extract_layer_filament_usage_from_3mf,
                    get_cumulative_usage_at_layer,
                    mm_to_grams,
                )

                layer_usage = extract_layer_filament_usage_from_3mf(file_path)
                if layer_usage:
                    cumulative_mm = get_cumulative_usage_at_layer(layer_usage, current_layer)
                    filament_props = extract_filament_properties_from_3mf(file_path)
                    layer_grams = {}
                    for filament_id, mm_used in cumulative_mm.items():
                        slot_id = filament_id + 1  # 0-based to 1-based
                        props = filament_props.get(slot_id, {})
                        density = props.get("density", 1.24)
                        diameter = props.get("diameter", 1.75)
                        layer_grams[slot_id] = mm_to_grams(mm_used, diameter, density)
            except Exception:
                pass  # Fall back to linear scaling

    results = []

    for usage in filament_usage:
        slot_id = usage.get("slot_id", 0)
        used_g = usage.get("used_g", 0)
        if used_g <= 0:
            continue

        # --- Mid-print tray switch: split THIS slot's weight across its feeders ---
        this_slot_segments = slot_segments.get(slot_id) or []
        if len({t for t, _ in this_slot_segments}) > 1:
            # Compute total weight for this slot (same logic as normal path)
            if layer_grams and slot_id in layer_grams:
                total_weight = layer_grams[slot_id]
            else:
                total_weight = used_g * scale

            if total_weight <= 0:
                continue

            # Extract per-layer gcode for segment splitting
            split_layer_usage = None
            split_props: dict = {}
            try:
                from backend.app.utils.threemf_tools import (
                    extract_filament_properties_from_3mf,
                    extract_layer_filament_usage_from_3mf,
                    get_cumulative_usage_at_layer,
                    mm_to_grams,
                )

                split_layer_usage = extract_layer_filament_usage_from_3mf(file_path)
                filament_props = extract_filament_properties_from_3mf(file_path)
                split_props = filament_props.get(slot_id, {})
            except Exception:
                pass  # Fall back to linear splitting

            density = split_props.get("density", 1.24)
            diameter = split_props.get("diameter", 1.75)
            filament_id = slot_id - 1  # 0-based for gcode

            # Accumulate grams per feeder tray. A slot can feed the same tray in
            # more than one span (its colour prints, yields to another colour,
            # then resumes on the same tray), so aggregate before writing one
            # history row per spool. The last segment carries the rounding
            # remainder so the parts sum to exactly total_weight.
            seg_count = len(this_slot_segments)
            per_tray_grams: dict[int, float] = {}
            sum_previous = 0.0
            for seg_idx, (tray_global, seg_start_layer) in enumerate(this_slot_segments):
                is_last = seg_idx + 1 >= seg_count

                if is_last:
                    # Last segment: remainder to avoid rounding drift
                    segment_grams = total_weight - sum_previous
                elif split_layer_usage:
                    seg_end_layer = this_slot_segments[seg_idx + 1][1]
                    mm_at_start = get_cumulative_usage_at_layer(split_layer_usage, seg_start_layer).get(filament_id, 0)
                    mm_at_end = get_cumulative_usage_at_layer(split_layer_usage, seg_end_layer).get(filament_id, 0)
                    segment_grams = mm_to_grams(mm_at_end - mm_at_start, diameter, density)
                else:
                    # No per-layer data: linear fallback by layer ratio (#1771).
                    # Cascade denominators because firmware on some models (P1S
                    # observed) resets `total_layer_num` to 0 at print end —
                    # `last_layer_num` is the print's last-valid layer captured
                    # mid-print and survives that reset. Equal-split is the
                    # last-resort fence: still wrong, but bounded — never dumps
                    # the entire print onto the last segment, which was the
                    # original #1771 symptom for the reporter (P1S, AMS Backup
                    # fed from spool 1 then spool 2, all 260 g credited to
                    # spool 2 even though spool 1 had given up its 180 g).
                    seg_end_layer = this_slot_segments[seg_idx + 1][1]
                    denom = split_total_layers or last_layer_num
                    if denom > 0:
                        segment_grams = total_weight * (seg_end_layer - seg_start_layer) / denom
                    else:
                        # No layer information available from any source —
                        # spread evenly across segments.
                        segment_grams = total_weight / seg_count

                sum_previous += segment_grams
                if segment_grams > 0:
                    per_tray_grams[tray_global] = per_tray_grams.get(tray_global, 0.0) + segment_grams

            for tray_global, tray_grams in per_tray_grams.items():
                if tray_grams <= 0:
                    continue

                seg_ams_id, seg_tray_id = _global_tray_to_ams_key(tray_global)
                seg_key = (seg_ams_id, seg_tray_id)
                if seg_key in handled_trays:
                    continue

                logger.info(
                    "[UsageTracker] 3MF split: slot %d tray=%d (AMS%d-T%d) -> %.1fg",
                    slot_id,
                    tray_global,
                    seg_ams_id,
                    seg_tray_id,
                    tray_grams,
                )

                seg_spool_id = await _resolve_spool_id_for_tray(
                    printer_id=printer_id,
                    ams_id=seg_ams_id,
                    tray_id=seg_tray_id,
                    db=db,
                    spool_assignments_snapshot=spool_assignments,
                    print_started_at=print_started_at,
                )
                if seg_spool_id is None:
                    logger.info(
                        "[UsageTracker] 3MF split: no spool at printer %d AMS%d-T%d, skipping segment",
                        printer_id,
                        seg_ams_id,
                        seg_tray_id,
                    )
                    continue

                spool_result = await db.execute(select(Spool).where(Spool.id == seg_spool_id))
                spool = spool_result.scalar_one_or_none()
                if not spool:
                    continue

                spool.weight_used = (spool.weight_used or 0) + tray_grams
                spool.last_used = datetime.now(timezone.utc)

                percent = round(tray_grams / (spool.label_weight or 1000) * 100)

                cost = None
                cost_per_kg = spool.cost_per_kg if spool.cost_per_kg is not None else default_filament_cost
                if cost_per_kg > 0:
                    cost = round((tray_grams / 1000.0) * cost_per_kg, 2)

                history = SpoolUsageHistory(
                    spool_id=spool.id,
                    printer_id=printer_id,
                    print_name=print_name,
                    weight_used=round(tray_grams, 1),
                    percent_used=percent,
                    status=status,
                    cost=cost,
                    archive_id=archive_id,
                )
                db.add(history)

                handled_trays.add(seg_key)
                results.append(
                    {
                        "spool_id": spool.id,
                        "weight_used": round(tray_grams, 1),
                        "percent_used": percent,
                        "ams_id": seg_ams_id,
                        "tray_id": seg_tray_id,
                        "material": spool.material,
                        "cost": cost,
                        "slot_id": slot_id,
                        "color": _spool_color_to_hex(spool.rgba),
                    }
                )

                logger.info(
                    "[UsageTracker] Spool %d consumed %.1fg (3MF split) on printer %d AMS%d-T%d (%s)",
                    spool.id,
                    tray_grams,
                    printer_id,
                    seg_ams_id,
                    seg_tray_id,
                    status,
                )

            continue  # Skip normal single-tray processing for this slot

        # Map 3MF slot_id to physical (ams_id, tray_id) using resolved mapping
        if tray_now_override is not None:
            # Single-filament non-queue print: use actual tray from printer state
            global_tray_id = tray_now_override
        else:
            # Explicit mapping (print command, MQTT, queue, color match)
            global_tray_id = None
            if slot_to_tray and slot_id <= len(slot_to_tray):
                mapped = slot_to_tray[slot_id - 1]
                if isinstance(mapped, int) and mapped >= 0:
                    global_tray_id = mapped
            # Position-based default: sort available tray IDs so external spools (254/255)
            # naturally follow standard AMS trays, matching slicer slot numbering.
            #
            # Filter out AMS slots that have no spool loaded (empty `tray_type`) —
            # BambuStudio/OrcaSlicer compact the slot list when assigning filaments
            # and don't expose empty AMS slots to the user, so the slicer's 3MF
            # slot N maps to the Nth *loaded* tray, not the Nth physical position.
            # Without this filter a "3 AMS slots loaded + 1 empty + external"
            # layout routes the slicer's 4th filament to the empty AMS slot
            # instead of the external (#1607), and the external's spool usage
            # never gets recorded. vt_tray entries are already filtered the
            # same way inside `build_ams_tray_lookup` (line 174 checks
            # `tray_type`), so this just mirrors that for the AMS side.
            if global_tray_id is None:
                _state = printer_manager.get_status(printer_id)
                _raw = getattr(_state, "raw_data", None) if _state else None
                if _raw:
                    from backend.app.services.spoolman_tracking import build_ams_tray_lookup

                    _lookup = build_ams_tray_lookup(_raw)
                    available_trays = sorted(gid for gid, info in _lookup.items() if info.get("tray_type"))
                    if slot_id <= len(available_trays):
                        global_tray_id = available_trays[slot_id - 1]
            # Final fallback: slot_id - 1 (legacy, works for pure AMS without external spools)
            if global_tray_id is None:
                global_tray_id = slot_id - 1

        if global_tray_id >= 254:
            # External spool: ams_id=255 (sentinel), tray_id=slot index (0 or 1)
            ams_id = 255
            tray_id = global_tray_id - 254
        elif global_tray_id >= 128:
            ams_id = global_tray_id
            tray_id = 0
        else:
            ams_id = global_tray_id // 4
            tray_id = global_tray_id % 4

        logger.info(
            "[UsageTracker] 3MF: slot_id=%d -> global_tray=%d -> AMS%d-T%d (used_g=%.1f, tray_now_override=%s)",
            slot_id,
            global_tray_id,
            ams_id,
            tray_id,
            used_g,
            tray_now_override,
        )

        key = (ams_id, tray_id)
        if key in handled_trays:
            continue

        spool_id = await _resolve_spool_id_for_tray(
            printer_id=printer_id,
            ams_id=ams_id,
            tray_id=tray_id,
            db=db,
            spool_assignments_snapshot=spool_assignments,
            print_started_at=print_started_at,
        )
        if spool_id is None:
            logger.info("[UsageTracker] 3MF: no spool assignment at printer %d AMS%d-T%d", printer_id, ams_id, tray_id)
            continue

        # Load spool
        spool_result = await db.execute(select(Spool).where(Spool.id == spool_id))
        spool = spool_result.scalar_one_or_none()
        if not spool:
            continue

        # Use per-layer grams if available, otherwise linear scale
        if layer_grams and slot_id in layer_grams:
            weight_grams = layer_grams[slot_id]
        else:
            weight_grams = used_g * scale

        if weight_grams <= 0:
            continue

        # Update spool
        spool.weight_used = (spool.weight_used or 0) + weight_grams
        spool.last_used = datetime.now(timezone.utc)

        percent = round(weight_grams / (spool.label_weight or 1000) * 100)

        # Calculate cost for this usage
        cost = None
        cost_per_kg = spool.cost_per_kg if spool.cost_per_kg is not None else default_filament_cost
        if cost_per_kg > 0:
            cost = round((weight_grams / 1000.0) * cost_per_kg, 2)

        # Insert usage history record
        history = SpoolUsageHistory(
            spool_id=spool.id,
            printer_id=printer_id,
            print_name=print_name,
            weight_used=round(weight_grams, 1),
            percent_used=percent,
            status=status,
            cost=cost,
            archive_id=archive_id,
        )
        db.add(history)

        handled_trays.add(key)
        results.append(
            {
                "spool_id": spool.id,
                "weight_used": round(weight_grams, 1),
                "percent_used": percent,
                "ams_id": ams_id,
                "tray_id": tray_id,
                "material": spool.material,
                "cost": cost,
                "slot_id": slot_id,
                "color": _spool_color_to_hex(spool.rgba),
            }
        )

        # Determine mapping source for debug logging
        if tray_now_override is not None:
            map_src = ", tray_now"
        elif mapping_source:
            map_src = f", {mapping_source}_map"
        else:
            map_src = ""
        logger.info(
            "[UsageTracker] Spool %d consumed %.1fg (3MF%s%s) on printer %d AMS%d-T%d (%s)",
            spool.id,
            weight_grams,
            " per-layer" if (layer_grams and slot_id in layer_grams) else (f" scaled {scale:.0%}" if scale < 1 else ""),
            map_src,
            printer_id,
            ams_id,
            tray_id,
            status,
        )

    # --- Adopt the matched inventory spools' colours for the archive (#1494) ---
    # The archive's filament_color was set from the slicer's 3MF at creation
    # time; now that every used slot has been resolved to an inventory spool,
    # the curated spool colour is authoritative. Committed by the caller's
    # `if results: await db.commit()`.
    if archive is not None:
        spool_colors = _archive_colors_from_spools(filament_usage, results)
        if spool_colors:
            joined = ",".join(spool_colors)
            if joined != archive.filament_color:
                logger.info(
                    "[UsageTracker] 3MF: archive %s filament_color %r -> %r (from inventory spools)",
                    archive_id,
                    archive.filament_color,
                    joined,
                )
                archive.filament_color = joined

    return results
