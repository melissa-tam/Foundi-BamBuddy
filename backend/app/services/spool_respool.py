"""Reused Bambu RFID tag → spent-certain auto re-spool (fork farm feature).

The farm refills spent Bambu 1 kg rolls by peeling the RFID tag onto a fresh
third-party spool. The AMS then auto-identifies the filament, but Bambuddy's
spool ledger would otherwise map the tag to the SPENT donor row (weight_used ≈
1000 g), silently stalling the lights-out queue on the #1496 filament-deficit
guard. This module is the single owner of the re-spool operation and its three
certainty tiers:

* **Tier 1 — spent-certain marking** (`mark_spent_on_runout`,
  `mark_spent_on_slot_runout`, `sample_status_push` + `confirm_backup_swaps`): a
  hardware runout signal (unrescued runout HMS / the firmware's own auto-switch
  statement / seamless AMS backup-swap) stamps ``spool.spent_at`` — the certainty
  key. Never set by gram estimates. The backup-swap detector is a SAMPLER/CONFIRMER
  pair, not one call: the sync sampler runs on every status push (~1 Hz) because the
  tray_now edge it watches is invisible to the AMS-hash-gated callback, and only a
  confirmed departure pays for a DB session.
* **Tier 2 — automatic re-spool** (`maybe_auto_or_prompt_respool`): a tag arrival
  resolving to a spent, LOADED tray physically cannot be the old (empty) spool,
  so it re-spools with no operator involvement — unless a standing "Same spool"
  dismissal still holds for the slot (see below).
* **Tier 3 — one-click prompt** (`maybe_auto_or_prompt_respool`): uncertain cases
  (spent_at NULL) broadcast a ``respool_prompt`` WS event mirroring the
  ``unknown_tag`` flow — but ONLY with physical evidence that the roll could have
  changed (a recent presence cycle on the slot, :func:`_swap_evidence`). A merely
  run-down seated spool, and an impossible ledger row, prompt nothing.

"Same spool" is honored PER PHYSICAL CYCLE for the spent tier: once the operator
answers "Same spool" (``respool_dismissed_at`` stamped), the whole spent branch —
Tier-2 auto AND any spent-tier prompt — stays suppressed until a qualified ≥5 s
presence cycle occurs on the slot AFTER the answer (:func:`_dismissal_stands`).
Replacing the roll is such a cycle, so genuine exhaustion still surfaces, but a
standing false spent stamp stops re-reacting (and, with auto on, stops minting
phantom fresh rows) the moment it is dismissed.

The core operation `respool_tag` disposes the donor, mints a fresh full
third-party spool (weight_locked, spent_at NULL), copies K-profiles, re-assigns
the slot and releases low-spool-staged farm items. All entry points no-op when
Spoolman owns the spool lifecycle (``spoolman_enabled == "true"``).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from backend.app.core.websocket import ws_manager
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_k_profile import SpoolKProfile
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services.hms_errors import hms_short_code
from backend.app.services.spool_tag_matcher import (
    ZERO_TAG_UID,
    ZERO_TRAY_UUID,
    auto_assign_spool,
    get_spool_by_tag,
    is_valid_tag,
    parse_tray_fields,
)
from backend.app.utils.tag_normalization import normalize_tag_uid, normalize_tray_uuid

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Tag vendor marker written on every re-spooled row. Single origin of truth so
# the sibling-tag guard and the observability hook agree on the classification.
RESPOOL_TAG_TYPE = "bambulab_reused"

# Hardware runout HMS short codes that mean "the AMS physically saw the filament
# end" — printer-side (0300_8004) and per-AMS-slot (07xx_8011). Curated to the
# same family already in main._HMS_FAILURE_REASONS; the slot-from-attr decode is
# hardware-probe-pinned, so we resolve WHICH tray ran out via the dispatched farm
# ams_mapping / live tray_now instead of the HMS attr.
RUNOUT_HMS_CODES: frozenset[str] = frozenset(
    {
        "0300_8004",
        "0700_8011",
        "0701_8011",
        "0702_8011",
        "0703_8011",
        "0704_8011",
        "0705_8011",
        "0706_8011",
        "0707_8011",
    }
)

# Per-printer last-seen loaded tray (global id) for the backup-swap detector.
# Module-level edge state matching the fork's other event-edge bookkeeping
# (farm_staging._tray_signatures). Lost on restart — worst case is one missed
# swap edge, which falls through to the Tier-3 prompt on reuse (fail-safe).
_last_tray_now: dict[int, int] = {}

# Backup-swap corroboration state (2026-07-19 incident). The bare last-tray edge
# false-fired twice: (a) OUR OWN recovery/UI swap looked like a firmware runout
# switch (006), and (b) a transient tray_now walk during the firmware's own runout
# handling stamped a slot that never fed (011). Two structures kill both modes:
#
#  * ``_commanded_loads[pid] = (target_tray, monotonic)`` — a load WE issued
#    (recovery load step / the /ams/load route). An edge whose NEW tray matches an
#    unexpired marker is our own swap and never stamps the departed spool.
#  * ``_stable_feeder[pid]`` — the tray_now value observed held unchanged for
#    ``_SWAP_CONFIRM_S`` during RUNNING; only an edge DEPARTING it opens a pending
#    swap, so the runout-time tray walk (whose values are never stable) can't. A
#    ``_pending_swaps[pid] = (departed, new, monotonic)`` confirms into a spent
#    stamp only after the new tray feeds stably that long with the departed still
#    present. ``_feeder_since`` tracks the held-unchanged window.
#
# All are process-lifetime like ``_last_tray_now``; a restart loses them and the
# next reuse falls through to the Tier-3 prompt (documented residual).
_stable_feeder: dict[int, int] = {}
_feeder_since: dict[int, tuple[int, float]] = {}
_pending_swaps: dict[int, tuple[int, int, float]] = {}
_commanded_loads: dict[int, tuple[int, float]] = {}

# Sentinel distinguishing "no backup-swap sample recorded yet" from a genuine
# subtask_id of ``None`` (an idle / degenerate-echo push), so the FIRST sample per
# printer is treated as a job boundary (which merely seeds).
_NO_JOB = object()

# Per-printer job identity (``subtask_id``) observed at the last backup-swap sample
# (2026-07-20 false-spent incident). The edge dicts above are keyed by printer only,
# so their state outlives a job boundary whenever no not-RUNNING AMS delta happens to
# arrive between jobs — idle gaps emit few/no AMS deltas, and eject jobs run
# state=RUNNING so the ``if not running`` cleanup never fires. A feeder change chosen
# by the NEXT job's dispatch mapping then looked identical to a mid-job firmware backup
# switch and stamped the departed spool spent (spool 106, printer 5, AMS0-T0, 02:40).
# This records the job each edge sample was taken under so :func:`sample_status_push`
# recognises a changed subtask_id (incl. ``None``↔value) as a boundary and DISCARDS the
# cross-job edge instead of confirming a swap. Process-lifetime like the edge dicts;
# cleared by :func:`_reset_state`. The job-boundary reset hooks in ``main`` clear the
# edge dicts but deliberately NOT this marker — it is this belt-and-braces layer's own.
_last_sample_job: dict[int, object] = {}

# A hardware runout that stamps a spool spent while its gram ledger still shows more
# than this remaining is a drift / initial-state signal worth surfacing in triage
# (reused core, a mid-life row minted as full, or accrual drift) — not silent loss.
# The spent stamp still stands (hardware evidence is authoritative); this only logs.
_SPENT_LEDGER_REMAIN_WARN_G = 150.0

# Seconds a tray_now value must hold unchanged during RUNNING to count as the stable
# feeder, and for a pending backup swap to confirm into a spent stamp.
_SWAP_CONFIRM_S = 60.0
# A commanded-load marker older than this is stale (the load never took / a much
# later unrelated edge); it stops suppressing.
_COMMANDED_LOAD_TTL_S = 600

# Per-printer dedup for `respool_prompt` WS broadcasts, keyed
# (ams_id, tray_id) -> (tag_uid, tray_uuid). Mirrors main._unknown_tag_last_broadcast:
# re-broadcast only when the tag tuple changes for the slot; cleared when the
# slot goes empty so remove + reinsert re-prompts.
_respool_prompt_dedup: dict[int, dict[tuple[int, int], tuple[str, str]]] = {}

# Remain-jump corroboration ledger, keyed (printer_id, ams_id, tray_id) ->
# (first_seen_monotonic, observation_count). A single AMS push showing a remain
# jump is not evidence — the reading can be mid-identify garbage or a one-off
# firmware artefact — so a jump counts only once it has held across
# ``_JUMP_MIN_PUSHES`` observations spanning ``_JUMP_STABLE_S``. The entry is
# dropped the moment the jump stops reading (the condition must hold, not merely
# have occurred) and on the slot-empty edge (:func:`clear_respool_prompt_dedup`).
# Process-lifetime like the edge dicts above: a restart simply re-corroborates.
_jump_seen: dict[tuple[int, int, int], tuple[float, int]] = {}

# Per-incident spent-stamp dedup, keyed (printer_id, subtask_id, global_tray): one
# spent stamp per tray per job. A re-raised runout HMS on the SAME job/tray must
# not stamp again — otherwise a fresh spool the operator just inserted (auto-minted
# and re-assigned to the same slot) gets stamped SPENT with a fabricated
# label-floored weight (production 2026-07-17 18:56: new spool 73 stamped 1000 g
# spent 7 s after insertion). Key-scoped by subtask_id so a genuinely new job
# naturally misses; process-lifetime like the other edge dicts above, cleared by
# :func:`_reset_state`.
_spent_dedup: set[tuple[int, object, int]] = set()

# S1 restart-replay suppression. The HMS dedup (``services.notify_dedup``) recognises
# a standing code across a restart only when it was ALERTED on before (durable ledger
# row); a never-notified code (no description / info severity) still replays as "new"
# on the next push — and ``mark_spent_on_runout`` would re-stamp spent on whatever spool is bound to the
# tray NOW, which after an operator swap during the pause is a FRESH roll (production
# 2026-07-17 18:56: a fresh spool stamped spent+1000 g 7 s after insertion). These
# two structures record the runout codes ALREADY LIVE at the first status push per
# printer (via :func:`note_status_push`) so a replayed pre-restart runout is skipped,
# while a genuinely-new runout appearing later is absent from the seed and stamps
# normally. Process-lifetime like the edge dicts above; cleared by :func:`_reset_state`.
_runout_seeded: set[int] = set()
_seeded_runout_codes: dict[int, set[str]] = {}

# S1 for the slot-attributed auto-switch family (:func:`mark_spent_on_slot_runout`),
# keyed by the LOSSLESS ``full_code`` rather than the short code its sibling above
# uses. The short code is unusable as identity here: ``hms_short_code`` keeps only
# ``code & 0xFFFF``, dropping the code-word high bits that separate the demand
# (0x00020001) from the purge notice (0x00030001) — both render ``07xx_0001`` — and
# it keeps only ``attr >> 16``, dropping the slot byte, so all four slots of one AMS
# render identically. Seeding on the short code would therefore suppress a genuine
# NEW runout on a DIFFERENT slot, which is exactly the chained multi-slot case this
# trigger exists for. Same lifecycle as the sets above: seeded once per printer per
# process by :func:`note_status_push`, intersection-pruned on every later push.
_seeded_slot_runout_full: dict[int, set[str]] = {}


def _reset_state() -> None:
    """Test hook: clear module-level edge/dedup state between cases."""
    _last_tray_now.clear()
    _stable_feeder.clear()
    _feeder_since.clear()
    _pending_swaps.clear()
    _commanded_loads.clear()
    _last_sample_job.clear()
    _respool_prompt_dedup.clear()
    _jump_seen.clear()
    _spent_dedup.clear()
    _runout_seeded.clear()
    _seeded_runout_codes.clear()
    _seeded_slot_runout_full.clear()


def _monotonic() -> float:
    """Monotonic clock indirection so tests can drive the swap-confirm windows
    without wall-clock waits (mirrors spool_recovery._now)."""
    return time.monotonic()


def note_commanded_load(printer_id: int, target_tray: int) -> None:
    """Record that WE just issued an AMS load of ``target_tray`` on ``printer_id``.

    Called by the two farm load paths (spool_recovery's load step + the printers
    ``/ams/load`` route) BEFORE the MQTT publish. The backup-swap detector consumes
    a marker whose target matches the resulting tray_now edge, so our own recovery /
    operator swaps can never be mistaken for a firmware runout and spend the
    departed spool (the 006 false-stamp mode)."""
    _commanded_loads[printer_id] = (target_tray, _monotonic())


def reset_swap_edge_state(printer_id: int) -> None:
    """Clear the backup-swap edge state for ``printer_id`` at a job boundary.

    Called (guarded) from ``main.on_print_start`` — BEFORE its eject short-circuit,
    because an eject job is a job boundary too — and ``main.on_print_complete`` so the
    per-printer edge bookkeeping never carries from one print into the next: a feeder
    change chosen by the NEXT job's dispatch mapping must not read as a mid-job
    firmware backup switch and stamp the departed spool spent (the 2026-07-20
    false-spent incident). After a reset the next AMS-delta push merely re-seeds
    ``_last_tray_now`` (prev ``None`` → no edge possible). Drops only the four swap
    trackers; :data:`_last_sample_job` is owned by the belt-and-braces boundary check
    in :func:`sample_status_push` and is intentionally left intact. Idempotent; pure
    in-memory; never raises.
    """
    _last_tray_now.pop(printer_id, None)
    _feeder_since.pop(printer_id, None)
    _stable_feeder.pop(printer_id, None)
    _pending_swaps.pop(printer_id, None)


class RespoolError(Exception):
    """Re-spool failure carrying an HTTP status + operator-facing detail.

    The route maps this straight onto an HTTPException; the auto path catches it
    to fall back to the prompt tier instead of raising into the AMS callback.
    """

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class RespoolSiblingConflict(RespoolError):
    """The tray_uuid-matching active row is a DIFFERENT reused-tag spool.

    Bambu rolls carry two RFID tags sharing one tray_uuid. When the donor's
    sibling tag already lives on another third-party spool, proceeding would
    silently merge two physical spools (get_spool_by_tag prefers tray_uuid), so
    we refuse. Carries the conflicting spool id for an actionable message.
    """

    def __init__(self, conflicting_spool_id: int):
        self.conflicting_spool_id = conflicting_spool_id
        super().__init__(
            409,
            (
                f"Tray UUID already belongs to re-spooled spool #{conflicting_spool_id} via its sibling tag. "
                "Use ONE tag per donor roll — discard the second tag and re-spool with a different donor roll's tag."
            ),
        )


# --- setting helpers --------------------------------------------------------


async def _spoolman_enabled(db: AsyncSession) -> bool:
    from backend.app.api.routes.settings import get_setting

    value = await get_setting(db, "spoolman_enabled")
    return bool(value) and value.lower() == "true"


async def _respool_auto_enabled(db: AsyncSession) -> bool:
    """Whether Tier-2 automatic re-spool is on. Absent → False (operator directive:
    the farm does NOT reuse tags yet, so a spent+loaded arrival prompts by default)."""
    from backend.app.api.routes.settings import get_setting

    value = await get_setting(db, "respool_auto_enabled")
    return bool(value) and value.strip().lower() == "true"


async def _respool_last_brand(db: AsyncSession) -> str:
    from backend.app.api.routes.settings import get_setting

    return (await get_setting(db, "respool_last_brand")) or ""


async def _respool_prompt_threshold_g(db: AsyncSession) -> int:
    from backend.app.api.routes.settings import get_setting

    raw = await get_setting(db, "respool_prompt_threshold_g")
    try:
        return int(raw) if raw is not None else 30
    except (TypeError, ValueError):
        return 30


# --- tray geometry helpers --------------------------------------------------


def _decode_global_tray(global_tray: int | None) -> tuple[int | None, int | None]:
    """Decode a global tray id to (ams_id, tray_id) for SpoolAssignment lookup.

    Mirrors the encoding used across bambu_mqtt / main: regular AMS
    ``global = ams_id*4 + slot``; AMS-HT (128-191) reports ``global == ams_id``
    (single tray); external vt_tray 254/255 maps to ams_id=255 slot 0/1 (the
    ``tray_id + 254`` convention from the auto-unlink path).
    """
    if global_tray is None or global_tray < 0:
        return (None, None)
    if global_tray in (254, 255):
        return (255, global_tray - 254)
    if 128 <= global_tray <= 191:
        return (global_tray, 0)
    if global_tray <= 127:
        return (global_tray // 4, global_tray % 4)
    return (None, None)


def _iter_ams_units(state) -> list:
    """Normalize the AMS payload in ``state.raw_data`` to a list of AMS units."""
    if not state or not getattr(state, "raw_data", None):
        return []
    ams_data = state.raw_data.get("ams")
    if isinstance(ams_data, list):
        return ams_data
    if isinstance(ams_data, dict):
        if isinstance(ams_data.get("ams"), list):
            return ams_data["ams"]
        if "tray" in ams_data:
            return [{"id": 0, "tray": ams_data.get("tray", [])}]
    return []


def _resolve_live_tray(state, ams_id: int, tray_id: int) -> dict | None:
    """Find the live tray dict for (ams_id, tray_id) from a printer state.

    Handles the external vt_tray slot (ams_id=255) via the ``tray_id + 254``
    global-id convention (main.on_ams_change) and regular AMS units via the same
    normalization ``create_spool_from_slot`` uses.
    """
    if not state or not getattr(state, "raw_data", None):
        return None
    if ams_id == 255:
        vt_tray = state.raw_data.get("vt_tray") or []
        ext_id = tray_id + 254  # 0→254, 1→255
        for vt in vt_tray:
            if isinstance(vt, dict) and int(vt.get("id", 254)) == ext_id:
                return vt
        return None
    for unit in _iter_ams_units(state):
        if not isinstance(unit, dict) or int(unit.get("id", -1)) != ams_id:
            continue
        for tray in unit.get("tray", []):
            if isinstance(tray, dict) and int(tray.get("id", -1)) == tray_id:
                return tray
    return None


def _tray_loaded(tray: dict) -> bool:
    """Filament-loaded heuristic — mirrors main.on_ams_change (:1643 semantics).

    state == 11 (fed to extruder) OR a non-empty tray_type when state is NOT one
    of the firmware's explicit empty signals (9, 10). A spent spool re-inserted
    without refill reads present-but-not-loaded → False → no auto trigger.
    """
    cur_state = tray.get("state")
    cur_type = (tray.get("tray_type") or "").strip()
    return cur_state == 11 or (cur_state not in (9, 10) and bool(cur_type))


# --- Tier 1: spent-certain marking -----------------------------------------


async def _mark_tray_spent(db: AsyncSession, printer_id: int, global_tray: int) -> Spool | None:
    """Stamp spent_at on the spool assigned to the decoded slot. Idempotent."""
    ams_id, tray_id = _decode_global_tray(global_tray)
    if ams_id is None:
        return None
    result = await db.execute(
        select(SpoolAssignment)
        .options(selectinload(SpoolAssignment.spool))
        .where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    assignment = result.scalar_one_or_none()
    if assignment is None or assignment.spool is None:
        return None
    spool = assignment.spool
    if spool.spent_at is not None:
        return spool  # idempotent — already marked spent
    spool.spent_at = datetime.utcnow()
    # DO NOT floor weight_used to the label. Emptiness is DERIVED from spent_at at
    # every load-bearing consumer (filament_deficit removes spent rows from the
    # pool; spool_selection's SlotInventory.spent hard-excludes), so the floor was
    # pure loss: it destroyed the true gram ledger and made a FALSE spent stamp
    # unrecoverable (2026-07-19). Leaving grams intact lets the evidence-gated
    # dismissal un-spend restore the exact prior weight losslessly.
    #
    # Surface a spent stamp landing on a fat ledger remainder: a hardware runout with
    # lots of grams still on the books is the drift / initial-state contradiction (the
    # tagged path already raises the trigger=spent respool prompt and the tagless path
    # the next-cycle W5 fresh-roll prompt, but neither names THIS gap). No new prompt
    # machinery — the WARNING is the triage floor.
    remaining_g = float(spool.label_weight or 0) - float(spool.weight_used or 0)
    if remaining_g > _SPENT_LEDGER_REMAIN_WARN_G:
        logger.warning(
            "Spool %d marked spent (printer %d AMS%d-T%d) with %.0f g still on the ledger "
            "(> %.0f g floor) — hardware runout with a fat remainder signals drift or an "
            "initial-state error (reused core / mid-life row minted as full), not silent loss",
            spool.id,
            printer_id,
            ams_id,
            tray_id,
            remaining_g,
            _SPENT_LEDGER_REMAIN_WARN_G,
        )
    await db.commit()
    logger.info(
        "Marked spool %d spent (printer %d AMS%d-T%d, hardware runout)",
        spool.id,
        printer_id,
        ams_id,
        tray_id,
    )
    return spool


def _runout_slot_global_tray(state) -> int | None:
    """The global tray the firmware itself named as run-out, or ``None``.

    The ``0700_2X00`` runout family encodes the exhausted AMS+slot in its ``attr``
    ("AMS A Slot 3 filament has run out …") — proven correct on the 011 incident
    while tray_now-edge inference misfired. Decode every live HMS entry through the
    pure :func:`hms_errors.runout_slot_from_hms`; the first hit wins. Fails closed
    (``None``) when ``hms_errors`` is absent / not a list (so tray_now/mapping stays
    the fallback for the slot-agnostic 8011-only case and for MagicMock states)."""
    from backend.app.services.hms_errors import _code_word, runout_slot_from_hms

    hms_list = getattr(state, "hms_errors", None)
    if not isinstance(hms_list, list):
        return None
    for e in hms_list:
        try:
            hit = runout_slot_from_hms(int(getattr(e, "attr", 0) or 0), _code_word(getattr(e, "code", 0)))
        except Exception:  # noqa: BLE001 — a malformed HMS entry must not crash resolution
            continue
        if hit is not None:
            ams_id, tray_id = hit
            return ams_id * 4 + tray_id
    return None


async def _resolve_exhausted_tray(db: AsyncSession, printer_id: int, state) -> int | None:
    """Which tray ran out.

    Firmware slot attribution is PRIMARY, and within it the CURRENT DEMAND outranks the
    first decodable entry. Order:

    1. :func:`hms_errors.current_runout_demand` — the slot the firmware is asking to
       have FILLED right now. This trigger only ever fires on the UNRESCUED families
       (``RUNOUT_HMS_CODES``), and by definition the unrescued slot is the demanded one.
    2. :func:`_runout_slot_global_tray` — first-hit decode over the whole slot-attributed
       family, for an unrescued runout that names a slot but raises no standing demand.
    3. Inference: prefer the live feeding ``tray_now`` over the dispatched farm
       ams_mapping for a single-feeder job (the mapping can be stale after a firmware
       backup-switch / operator reload), falling back to the mapping when ``tray_now``
       is unloaded/unknown (255/None). ``last_loaded_tray`` remains un-consulted (the
       firmware-named slot supersedes it); the multi-feeder fail-safe is unchanged.

    Step 1 is load-bearing for the CHAINED case. The firmware APPENDS newer faults, so
    the first-hit decode returns the OLDEST slot-attributed entry — after a rescue-then-
    unrescued sequence that is the slot the auto-switch trigger has ALREADY stamped,
    whereupon ``_spent_dedup`` swallows this stamp and the actually-unrescued roll is
    never marked. 006-H2S 2026-07-26 carried exactly that list shape (an older
    auto-switched slot-1 entry ahead of the standing slot-3 demand).

    The ``isinstance`` guard keeps the decode fail-closed on an absent / non-list
    ``hms_errors`` (MagicMock states in tests, a degenerate push in production), so
    inference stays the fallback rather than raising into a callback."""
    hms_list = getattr(state, "hms_errors", None)
    if isinstance(hms_list, list):
        from backend.app.services.hms_errors import current_runout_demand

        demanded = current_runout_demand(hms_list)
        if demanded is not None:
            return demanded[0] * 4 + demanded[1]
    decoded = _runout_slot_global_tray(state)
    if decoded is not None:
        return decoded
    result = await db.execute(
        select(PrintQueueItem)
        .join(PrintBatch, PrintQueueItem.batch_id == PrintBatch.id)
        .where(
            PrintQueueItem.printer_id == printer_id,
            PrintQueueItem.status == "printing",
            PrintQueueItem.ams_mapping.is_not(None),
            PrintBatch.sku_file_id.is_not(None),
        )
        .order_by(PrintQueueItem.started_at.desc())
        .limit(1)
    )
    item = result.scalar_one_or_none()
    tray_now = getattr(state, "tray_now", None)
    live_ok = tray_now is not None and 0 <= tray_now <= 254
    if item and item.ams_mapping:
        try:
            mapping = json.loads(item.ams_mapping)
            feeders = [int(v) for v in mapping if isinstance(v, (int, float)) and int(v) >= 0]
        except (ValueError, TypeError):
            feeders = []
        if len(feeders) == 1:
            # Single-feeder farm job: the live feeding tray is authoritative; the
            # mapping is only a fallback for an unloaded/unknown tray_now.
            return tray_now if live_ok else feeders[0]
        if feeders:
            # Multi-filament job: the mapping alone can't say WHICH feeder ran
            # out. Trust the live tray_now only when it is one of the job's
            # feeders; otherwise mark nothing (fail-safe — a wrong spent stamp
            # would auto-reset a half-full spool to fresh on its next arrival).
            if tray_now is not None and tray_now in feeders:
                return tray_now
            return None
    if tray_now is not None and 0 <= tray_now <= 254:
        return tray_now
    return None


def _live_runout_codes(state) -> set[str]:
    """Runout HMS short codes currently live on ``state`` — the same short-code
    derivation the runout hook / spool_recovery use (``hms_short_code(attr, code)``),
    intersected with :data:`RUNOUT_HMS_CODES`."""
    out: set[str] = set()
    for e in getattr(state, "hms_errors", None) or []:
        try:
            out.add(hms_short_code(e.attr, e.code))
        except Exception:  # noqa: BLE001 — a malformed HMS entry must not crash seeding
            continue
    return out & RUNOUT_HMS_CODES


def _live_slot_runout_full(state) -> set[str]:
    """``full_code``s of the slot-attributed AUTO-SWITCH runouts currently live on
    ``state`` — the S1 seed alphabet of :func:`mark_spent_on_slot_runout`.

    Two conjunctive conditions, matching that trigger's own filter exactly so the seed
    can never be narrower than what stamps: the code word is in the spent-evidence
    subset (:data:`hms_errors._RUNOUT_AUTO_SWITCH_SPENT_CODE32`) AND its attr decodes
    to a real slot. An entry whose slot fails to decode can never stamp, so seeding it
    would only risk suppressing an unrelated code that happens to share its full_code.
    """
    from backend.app.services.hms_errors import (
        _RUNOUT_AUTO_SWITCH_SPENT_CODE32,
        _code_word,
        runout_slot_from_hms,
    )

    out: set[str] = set()
    for e in getattr(state, "hms_errors", None) or []:
        try:
            code_word = _code_word(getattr(e, "code", 0))
            if code_word not in _RUNOUT_AUTO_SWITCH_SPENT_CODE32:
                continue
            if runout_slot_from_hms(int(getattr(e, "attr", 0) or 0), code_word) is None:
                continue
            out.add(str(getattr(e, "full_code", "") or ""))
        except Exception:  # noqa: BLE001 — a malformed HMS entry must not crash seeding
            continue
    return out


def note_status_push(printer_id: int, state) -> None:
    """Seed / maintain the per-printer restart-replay runout-suppression set (S1).

    Called (guarded) from ``main.on_printer_status_change`` on every status push,
    BEFORE the runout hook — hence outside the "HMS present" branch, so the FIRST
    push per printer seeds even with zero HMS. That first push records the runout
    codes live at that instant into ``_seeded_runout_codes[printer_id]``: after a
    restart main lost its HMS dedup, so any code still live now would otherwise
    replay as "new" and mis-stamp a swapped-in fresh spool. Every LATER push drops
    seeded codes no longer live, so a genuine recurrence stamps normally; a code that
    first appears only AFTER seeding is never added here → correctly treated as new.

    Accepted residual: a runout that fired entirely DURING server downtime (never
    observed live at the first push) never stamps spent — the tray's true state is
    unknowable across the gap, so we fail safe. That case is bounded by the pause-
    stall watchdog (the print sits PAUSEd and escalates) and the Tier-3 respool prompt
    when the reused tag next arrives. Pure set bookkeeping; the caller owns guarding.

    Maintains BOTH runout seeds off the same one-shot and the same "real report" gate:
    the short-code set for :func:`mark_spent_on_runout` and the full-code set for
    :func:`mark_spent_on_slot_runout`. One seeding pass, so the two triggers can never
    disagree about which push was the first — a second one-shot could latch on a
    different push and leave one family unguarded across the restart."""
    live = _live_runout_codes(state)
    live_slot_full = _live_slot_runout_full(state)
    if printer_id not in _runout_seeded:
        # The one-shot seed must capture a REAL printer report. A fresh
        # PrinterState defaults to state="unknown" and the connect-time
        # on_state_change broadcast fires before any report arrives — consuming
        # the seed there would record an empty set and let a still-live runout
        # replay as "new" on the next push (the exact mis-stamp this guards).
        # Stay unseeded until the push carries a known gcode_state.
        if (getattr(state, "state", None) or "unknown").lower() == "unknown":
            return
        _runout_seeded.add(printer_id)
        _seeded_runout_codes[printer_id] = set(live)
        _seeded_slot_runout_full[printer_id] = set(live_slot_full)
        return
    seeded = _seeded_runout_codes.get(printer_id)
    if seeded:
        # Drop any seeded code no longer live so a genuine recurrence later stamps.
        seeded.intersection_update(live)
    seeded_full = _seeded_slot_runout_full.get(printer_id)
    if seeded_full:
        seeded_full.intersection_update(live_slot_full)


async def mark_spent_on_runout(db: AsyncSession, printer_id: int, new_short_codes, state) -> Spool | None:
    """Tier 1: a NEW runout HMS code stamps spent_at on the exhausted tray's spool.

    Resolves the exhausted tray via the dispatched farm ``ams_mapping`` (the
    deterministic feeding tray) falling back to the live ``tray_now``. Idempotent:
    re-observing the code is a no-op once spent_at is set. No-op in Spoolman mode.
    Skips a restart-replayed runout (a code seeded live at the first push — see
    :func:`note_status_push`) so a swapped-in fresh spool is never mis-stamped.
    """
    if await _spoolman_enabled(db):
        return None
    triggering = set(new_short_codes) & RUNOUT_HMS_CODES
    if not triggering:
        return None
    # S1: a code already live at the first status push after a restart is a replay of
    # a PRE-restart runout (main lost its in-memory HMS dedup), NOT a fresh exhaustion.
    # Stamping now would mis-mark whatever spool is bound to the slot NOW — after an
    # operator swap during the pause that is a FRESH roll (the 18:56 misattribution).
    seeded = _seeded_runout_codes.get(printer_id)
    if seeded and triggering & seeded:
        logger.info(
            "Restart-replayed runout on printer %d (%s already live at first status push) — not stamping spent",
            printer_id,
            sorted(triggering & seeded),
        )
        return None
    global_tray = await _resolve_exhausted_tray(db, printer_id, state)
    if global_tray is None:
        return None
    # Incident dedup: one spent stamp per (printer, job, tray). A re-raised runout
    # on the same job/tray must not stamp the operator's freshly-inserted spool.
    subtask_id = getattr(state, "subtask_id", None)
    key = (printer_id, subtask_id, global_tray)
    if key in _spent_dedup:
        return None
    spool = await _mark_tray_spent(db, printer_id, global_tray)
    if spool is not None:
        _spent_dedup.add(key)
    return spool


async def mark_spent_on_slot_runout(db: AsyncSession, printer_id: int, events, state) -> list[Spool]:
    """Tier 1: a NEW firmware AUTO-SWITCH runout stamps spent_at on the slot IT named.

    The sibling of :func:`mark_spent_on_runout` for the case that trigger structurally
    cannot see: a runout the AMS backup RESCUED. ``RUNOUT_HMS_CODES`` is the UNRESCUED
    vocabulary (0300_8004 + 07xx_8011 — "insert filament", print held), so a successful
    auto-refill — which raises only the slot-attributed ``0700_2X00`` family and keeps
    printing — never stamped anything. Fleet evidence 2026-07-30: four confirmed
    auto-refills across three printers, zero spent stamps; the un-stamped rows then
    surfaced as spurious "Fresh roll?" prompts on the eventual physical swap
    (a SPENT row takes the silent spent→mint path instead).

    ``events`` are ``(full_code, attr, code_word)`` tuples the caller has already
    established are NEW this push. Attribution is ATTR-PRIMARY and PER EVENT — the
    firmware names the exhausted slot in the attr, and one print can chain several
    (005-H2S 2026-07-30 ran three rolls dry inside a single job, its completion gram
    split proving four trays fed). Resolving once per push, or through tray_now, would
    stamp only one of them; the per-event loop stamps each, and each has its own
    ``_spent_dedup`` key so a re-raise of the same slot in the same job still can't
    double-stamp the roll the operator has since inserted.

    Only :data:`hms_errors._RUNOUT_AUTO_SWITCH_SPENT_CODE32` may reach here — see that
    constant for why the bare demand (0x00020001) is NOT spent evidence: 006-H2S proved
    firmware can latch a bogus demand for a slot that never ran dry, and a demand-driven
    stamp would have archived a healthy roll.

    Returns the spools stamped by THIS call (empty when nothing qualified). No-op in
    Spoolman mode; skips S1 restart replays; delegates the stamp itself to the shared
    :func:`_mark_tray_spent` so the idempotency and fat-remainder WARNING are identical.
    """
    if await _spoolman_enabled(db):
        return []
    from backend.app.services.hms_errors import _RUNOUT_AUTO_SWITCH_SPENT_CODE32, runout_slot_from_hms

    seeded = _seeded_slot_runout_full.get(printer_id) or set()
    subtask_id = getattr(state, "subtask_id", None)
    stamped: list[Spool] = []
    for full_code, attr, code_word in events or []:
        # Re-assert the trigger vocabulary here, exactly as :func:`mark_spent_on_runout`
        # re-intersects RUNOUT_HMS_CODES despite its caller having filtered too. The
        # wider slot-attributed family decodes a slot just fine, so a caller that hands
        # over a bare DEMAND would silently archive a healthy roll — the 006 false-stamp
        # class. The narrow set is this function's contract, so it fails closed on it.
        if code_word not in _RUNOUT_AUTO_SWITCH_SPENT_CODE32:
            continue
        # S1: a code already live at the first status push after a restart is a replay
        # of a PRE-restart runout, NOT a fresh exhaustion — stamping now would mis-mark
        # whatever spool is bound to the slot NOW (a fresh roll after an operator swap).
        if full_code in seeded:
            logger.info(
                "Restart-replayed slot runout on printer %d (%s already live at first status push) "
                "— not stamping spent",
                printer_id,
                full_code,
            )
            continue
        slot = runout_slot_from_hms(attr, code_word)
        if slot is None:
            continue
        ams_id, tray_id = slot
        global_tray = ams_id * 4 + tray_id
        # Incident dedup: one spent stamp per (printer, job, tray), shared with the
        # unrescued trigger so a runout seen by BOTH families stamps exactly once.
        key = (printer_id, subtask_id, global_tray)
        if key in _spent_dedup:
            continue
        spool = await _mark_tray_spent(db, printer_id, global_tray)
        if spool is not None:
            _spent_dedup.add(key)
            stamped.append(spool)
    return stamped


def _consume_commanded_load(printer_id: int, current: int) -> bool:
    """True (consuming the marker) when ``current`` matches an unexpired load WE
    issued — our own recovery/UI swap, never a firmware runout. A stale marker is
    dropped so it can't suppress a later genuine switch."""
    marker = _commanded_loads.get(printer_id)
    if marker is None:
        return False
    target, ts = marker
    if _monotonic() - ts > _COMMANDED_LOAD_TTL_S:
        _commanded_loads.pop(printer_id, None)
        return False
    if target == current:
        _commanded_loads.pop(printer_id, None)
        return True
    return False


def _update_stable_feeder(printer_id: int, current: int) -> None:
    """Track the tray_now value held unchanged ≥ ``_SWAP_CONFIRM_S`` during RUNNING
    as the confirmed stable feeder. A transient runout-time tray walk (011) never
    holds a value long enough to qualify, so it can never open a pending swap."""
    seen = _feeder_since.get(printer_id)
    now = _monotonic()
    if seen is None or seen[0] != current:
        _feeder_since[printer_id] = (current, now)
        return
    if now - seen[1] >= _SWAP_CONFIRM_S and 0 <= current <= 253:
        _stable_feeder[printer_id] = current


def _resolve_pending_swap(printer_id: int, current: int, running: bool) -> int | None:
    """Resolve an open pending backup swap against the current push; returns the
    DEPARTED global tray when it confirms, else ``None``.

    CONFIRM the departed tray as run-dry when the new tray has fed stably for
    ``_SWAP_CONFIRM_S`` with the print still RUNNING and tray_now not returned to the
    departed feeder — a genuine firmware backup switch, the departed ran dry. The
    departed tray reading ABSENT at confirm time does NOT invalidate: a tagless roll
    run fully to empty passes its tail through, and the exist-bits wipe
    (``bambu_mqtt.apply_tray_exist_bits``) forces the emptied slot to state 9 / blank
    tray_type WITHIN the confirm window — so a departed-tray absence right after a
    mid-print backup switch IS the run-to-empty signal, not an ordinary unload (the
    2026-07-21 003-H2S incident, where dropping on absence left both run-dry rows
    unstamped). The rare proactive operator pull is covered by the fat-remainder
    WARNING in :func:`_mark_tray_spent` plus the "Same spool" un-spend path. Confirming
    on age alone also covers the "a new edge resolves the old first" case: once the
    window elapses the swap confirms even if tray_now has since moved off ``cur`` to a
    third tray, so the chained 1→0→3 double switch stamps both departed spools. DROP
    (never confirm) if the print left RUNNING, tray_now returned to the departed feeder
    (it's feeding again → it did not run out), or tray_now moved off ``cur`` before the
    window elapsed (transient walk). Otherwise keep waiting.

    Pure in-memory and synchronous: the stamp itself belongs to
    :func:`confirm_backup_swaps`, which owns the only DB session on this path."""
    pending = _pending_swaps.get(printer_id)
    if pending is None:
        return None
    prev, cur, opened_ts = pending
    # Invalidating conditions first — the swap never happened / can't be trusted.
    if (not running) or (current == prev):
        _pending_swaps.pop(printer_id, None)
        return None
    if _monotonic() - opened_ts >= _SWAP_CONFIRM_S:
        _pending_swaps.pop(printer_id, None)
        return prev
    if current != cur:
        _pending_swaps.pop(printer_id, None)  # moved off `cur` before confirming → transient
        return None
    return None  # still on `cur`, within the window → keep waiting


def sample_status_push(printer_id: int, state) -> list[int]:
    """Tier 1: seamless AMS backup-swap detector (runout with no HMS), corroborated —
    the SAMPLING half. Returns the departed global trays whose pending swap CONFIRMED
    on this push (hand them to :func:`confirm_backup_swaps`).

    Called on EVERY status push (``main.on_printer_status_change``, ~1 Hz per printer)
    beside :func:`note_status_push`, because that is the only cadence at which the
    signal exists. The AMS-change callback this detector used to hang off is gated on
    bambu_mqtt's AMS hash, and ``tray_now`` is deliberately NOT hashed
    (``bambu_mqtt._ams_hash``) — so a seamless auto-switch surfaced there only when the
    drained slot's exist-bit wipe happened to change the hash, and ``_update_stable_feeder``
    (two same-value observations ≥ ``_SWAP_CONFIRM_S`` apart) was routinely starved of
    the samples it needs to arm at all. Fleet evidence 2026-07-30/31: four confirmed
    firmware auto-refills, zero spent stamps.

    Hence: sync, pure in-memory, NO DB and NO awaits — a per-push hook may not open a
    session (the Spoolman gate that used to run per AMS change now lives in
    :func:`confirm_backup_swaps`, which only runs on a confirmation). False-fire gating
    is unchanged: our own commanded loads are suppressed
    (:func:`_consume_commanded_load`), and only an edge DEPARTING the confirmed stable
    feeder — held into a pending swap that confirms after ``_SWAP_CONFIRM_S`` — can
    qualify, so the runout-time tray walk can't.
    """
    current = getattr(state, "tray_now", 255)
    running = getattr(state, "state", None) == "RUNNING"

    # Belt-and-braces cross-job discard (2026-07-20). The primary guard is the
    # job-boundary reset hooked into main.on_print_start / on_print_complete; this
    # covers a missed or lagging hook. Edge state sampled under a DIFFERENT subtask_id
    # (a ``None``↔value change counts) belongs to another print, so reset it, re-seed
    # ``_last_tray_now`` from this push, and open NO pending swap on this call. A
    # genuine mid-job backup switch keeps the same subtask_id and falls through to the
    # detector below, confirming exactly as it does today.
    current_job = getattr(state, "subtask_id", None)
    if _last_sample_job.get(printer_id, _NO_JOB) != current_job:
        reset_swap_edge_state(printer_id)
        _last_sample_job[printer_id] = current_job
        _last_tray_now[printer_id] = current
        return []

    # Resolve any open pending swap against THIS push first (may confirm or drop). A
    # list because the caller's contract is uniform, never because one push can carry
    # two: a swap opened on this push can only confirm on a LATER one (the window is
    # checked against ``opened_ts``), so at most one tray departs per call.
    departed = _resolve_pending_swap(printer_id, current, running)
    confirmed: list[int] = [] if departed is None else [departed]

    prev = _last_tray_now.get(printer_id)
    _last_tray_now[printer_id] = current

    if not running:
        # Only meaningful mid-print; drop the stability trackers so the first
        # RUNNING push after an idle period can't fire a false swap.
        _feeder_since.pop(printer_id, None)
        _stable_feeder.pop(printer_id, None)
        return confirmed

    _update_stable_feeder(printer_id, current)

    if prev is None or prev == current:
        return confirmed
    if prev < 0 or prev >= 254:
        return confirmed  # departed from an unloaded / external sentinel — not a swap edge
    if not (0 <= current <= 253):
        return confirmed  # switched to unloaded/external, not an AMS backup switch
    if _consume_commanded_load(printer_id, current):
        return confirmed  # our own recovery/UI swap — never a firmware runout
    if _stable_feeder.get(printer_id) != prev:
        return confirmed  # departed tray was not the stable feeder → transient walk edge

    # NO open-time presence check on ``prev``. The line above already guarantees it is
    # the CONFIRMED stable feeder, and a stable feeder that reads absent at the edge IS
    # the run-to-empty signature — the exist-bit wipe lands with, or before, the very
    # push that makes the edge visible at all. Vetoing on absence here therefore killed
    # precisely the genuine run-dry detections it was meant to filter (fleet evidence
    # 2026-07-30/31: four auto-refills, zero stamps); the ordinary-unload case it aimed
    # at is covered instead by the fat-remainder WARNING plus the "Same spool" un-spend
    # path. :func:`_resolve_pending_swap` already states the same tolerance at confirm
    # time — the two ends of one window now agree.
    #
    # A qualifying edge off the stable feeder: open a pending swap. It confirms into a
    # spent stamp only if the new tray feeds stably for _SWAP_CONFIRM_S.
    _pending_swaps[printer_id] = (prev, current, _monotonic())
    return confirmed


async def confirm_backup_swaps(
    printer_id: int,
    departed_trays: list[int],
    *,
    session_factory: Callable | None = None,
) -> list[Spool]:
    """Tier 1 backup swap, the CONFIRMING half: stamp each departed tray's spool spent.

    Owns the only DB work on this path — its own session, because the per-push sampler
    that feeds it runs inside the status callback and must not hold one. The Spoolman
    gate lives HERE for the same reason: it is a settings read, and reading it on every
    push (~1 Hz × fleet) to answer a question that matters only on a confirmation is
    pure load. Fire-and-forget from ``main``, so it is fully guarded and returns the
    stamped spools rather than raising (an exception would land in an orphaned task).

    ``session_factory`` exists so a caller can supply the session maker; ``None`` means
    the application's :data:`core.database.async_session` (imported lazily, matching the
    other own-session services).
    """
    if not departed_trays:
        return []
    stamped: list[Spool] = []
    try:
        if session_factory is None:
            from backend.app.core.database import async_session

            session_factory = async_session
        async with session_factory() as db:
            if await _spoolman_enabled(db):
                return []
            for global_tray in departed_trays:
                spool = await _mark_tray_spent(db, printer_id, global_tray)
                if spool is not None:
                    stamped.append(spool)
    except Exception as e:  # noqa: BLE001 — a fire-and-forget task must never raise
        logger.warning("Backup-swap confirm failed for printer %s trays %s: %s", printer_id, departed_trays, e)
    return stamped


async def capture_backup_swap(db: AsyncSession, printer_id: int, state) -> Spool | None:
    """Sample + confirm in one call against the CALLER's session — tests-only entry point.

    Production drives the two halves separately (:func:`sample_status_push` per status
    push, :func:`confirm_backup_swaps` as a fire-and-forget task) because the sampling
    cadence and the session-bearing work have different homes. This facade keeps the
    single-call shape the incident-pin suite exercises: it runs the same sampler and
    stamps through the same :func:`_mark_tray_spent`, so there is one implementation of
    the decision logic and the pins keep testing what production runs.

    Returns the first spool stamped by this call (or ``None``), matching the pre-split
    signature. No-op in Spoolman mode.
    """
    departed = sample_status_push(printer_id, state)
    if not departed:
        return None
    if await _spoolman_enabled(db):
        return None
    for global_tray in departed:
        spool = await _mark_tray_spent(db, printer_id, global_tray)
        if spool is not None:
            return spool
    return None


# --- Tier 2 / 3: automatic re-spool or prompt ------------------------------


def clear_respool_prompt_dedup(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Drop the cached per-slot prompt state (called when the slot reports empty).

    Clears BOTH per-slot memories in one place — the broadcast dedup tag and the
    remain-jump corroboration ledger (:data:`_jump_seen`). They share one lifetime:
    an emptied slot invalidates everything learned about the roll that was in it, so
    the next roll re-prompts and re-corroborates from scratch. Keeping the pair here
    means every caller of the empty edge (``main.on_ams_change``) and of a completed
    re-spool (:func:`respool_tag`) gets both without repeating itself.
    """
    per_printer = _respool_prompt_dedup.get(printer_id)
    if per_printer is not None:
        per_printer.pop((ams_id, tray_id), None)
    _jump_seen.pop((printer_id, ams_id, tray_id), None)


def _count_trays_in_ams(state, ams_id: int) -> int:
    for unit in _iter_ams_units(state):
        if isinstance(unit, dict) and int(unit.get("id", -1)) == ams_id:
            return len(unit.get("tray", []) or [])
    return 0


async def _classify_trigger(db: AsyncSession, donor: Spool) -> str:
    """Why this prompt fired: ``"spent"`` | ``"near_empty"`` | ``"remain_jump"``.

    Derived purely from DURABLE state (the spent stamp and the gram ledger vs the
    prompt threshold), never from the in-memory corroboration ledger, so a prompt
    replayed to a reconnecting client is labelled exactly as the live one was. The
    frontend picks its copy from this: ``near_empty`` gets "almost empty — replacing
    this roll?", the other two keep the reused-tag framing, which is what the
    operator's two false popups actually got wrong.

    Precedence mirrors the gate itself: a hardware-certain spent stamp outranks
    everything; otherwise a ledger reading at/below the threshold is the plain
    "almost empty" case and only a jump ABOVE it is reported as a reused core.
    """
    if donor.spent_at is not None:
        return "spent"
    remaining = (donor.label_weight or 0) - (donor.weight_used or 0)
    return "near_empty" if remaining <= await _respool_prompt_threshold_g(db) else "remain_jump"


async def _build_respool_prompt_payload(
    db: AsyncSession,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tray: dict,
    donor: Spool,
) -> dict:
    """Construct the frozen ``respool_prompt`` WS payload.

    Single origin shared by the live gate broadcast and the reconnect
    re-broadcast so the wire contract has exactly one definition — including the
    ``trigger`` label, which is therefore recomputed identically on replay.
    """
    from backend.app.services.printer_manager import printer_manager

    state = printer_manager.get_status(printer_id)
    tray_count = _count_trays_in_ams(state, ams_id) if ams_id != 255 else 0

    tray_weight = tray.get("tray_weight")
    try:
        label_weight_prefill = int(tray_weight) if tray_weight else int(donor.label_weight or 1000)
    except (TypeError, ValueError):
        label_weight_prefill = int(donor.label_weight or 1000)

    brand_prefill = (await _respool_last_brand(db)) or None
    donor_remaining = float((donor.label_weight or 0) - (donor.weight_used or 0))

    # Provenance (R5): so the operator can tell a stale question from a fresh
    # detection. Additive to the frozen contract; each field recomputes identically
    # on a reconnect replay from the same durable donor row + live tray (the
    # age fields excepted — they are inherently now-relative).
    spent_at = donor.spent_at
    spent_at_iso = spent_at.isoformat() if spent_at is not None else None
    spent_age_s = max(0.0, (datetime.utcnow() - spent_at).total_seconds()) if spent_at is not None else None

    # AMS live tray remain %, 1..100 or None — the same parse discipline
    # :func:`_remain_jump_reading` uses (integer %, out-of-range / garbage → None).
    ams_remain_pct: int | None = None
    try:
        remain_val = int(tray.get("remain"))
    except (TypeError, ValueError):
        remain_val = None
    if remain_val is not None and 1 <= remain_val <= 100:
        ams_remain_pct = remain_val

    # Ledger-implied remaining %, clamped at 0 like _remain_jump_reading's ledger_pct.
    label_weight = donor.label_weight or 0
    ledger_remain_pct = (
        max(0.0, (label_weight - (donor.weight_used or 0)) / label_weight * 100.0) if label_weight > 0 else None
    )

    bound_since_dt = donor.loaded_at or donor.first_loaded_at or donor.created_at
    bound_since = bound_since_dt.isoformat() if bound_since_dt is not None else None

    return {
        "type": "respool_prompt",
        "printer_id": printer_id,
        "ams_id": ams_id,
        "tray_id": tray_id,
        "tag_uid": (tray.get("tag_uid") or "") or None,
        "tray_uuid": (tray.get("tray_uuid") or "") or None,
        "tray_type": tray.get("tray_type") or None,
        "tray_color": tray.get("tray_color") or None,
        "tray_sub_brands": tray.get("tray_sub_brands") or None,
        "tray_count": tray_count,
        "donor_spool_id": donor.id,
        "donor_remaining_g": donor_remaining,
        "brand_prefill": brand_prefill,
        "label_weight_prefill": label_weight_prefill,
        "trigger": await _classify_trigger(db, donor),
        "spent_at": spent_at_iso,
        "spent_age_s": spent_age_s,
        "ams_remain_pct": ams_remain_pct,
        "ledger_remain_pct": ledger_remain_pct,
        "bound_since": bound_since,
    }


async def _broadcast_respool_prompt(
    db: AsyncSession,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tray: dict,
    donor: Spool,
) -> None:
    """Broadcast a deduped ``respool_prompt`` WS event (frozen contract)."""
    slot_key = (ams_id, tray_id)
    tag_uid = tray.get("tag_uid") or ""
    tray_uuid = tray.get("tray_uuid") or ""
    tag_key = (tag_uid, tray_uuid)
    per_printer = _respool_prompt_dedup.setdefault(printer_id, {})
    if per_printer.get(slot_key) == tag_key:
        return

    payload = await _build_respool_prompt_payload(db, printer_id, ams_id, tray_id, tray, donor)

    # Broadcast first; only commit the dedup if the WS write succeeds (mirrors
    # main._broadcast_unknown_tag so a failed push retries on the next tick).
    await ws_manager.broadcast(payload)
    per_printer[slot_key] = tag_key
    logger.info(
        "respool_prompt broadcast: printer=%d AMS=%d slot=%d donor=%d remaining=%.1fg trigger=%s",
        printer_id,
        ams_id,
        tray_id,
        donor.id,
        payload["donor_remaining_g"],
        payload["trigger"],
    )


async def pending_respool_prompts(db: AsyncSession) -> list[dict]:
    """Every still-unresolved ``respool_prompt``, as ready-to-send WS payloads.

    The ONE snapshot of what is outstanding, consumed by the reconnect replay
    (:func:`rebroadcast_unresolved_respool_prompts`) and the REST fallback
    (``GET /inventory/prompts/pending``) so the two can never disagree.

    Candidates come from the in-memory per-slot dedup (:data:`_respool_prompt_dedup`,
    the very records the live gate populates) — this tier keeps no durable state by
    design, unlike the tagless fresh-roll prompt's stamp. A dedup entry alone is NOT
    proof the prompt is still open: the durable answer lives in the DB, and the
    dismissal route stamps ``respool_dismissed_at`` WITHOUT clearing the dedup. So each
    slot is re-validated — the slot must still physically hold the SAME tag, and the
    tag's donor row must still resolve, be un-dismissed and un-archived. Stale entries
    are skipped, never mutated (the dedup clears on its own empty-slot edge). Empty in
    Spoolman mode. Raises nothing the callers do not already guard.
    """
    if await _spoolman_enabled(db):
        return []

    from backend.app.services.printer_manager import printer_manager

    # Snapshot the dedup so a concurrent AMS push mutating it cannot break iteration.
    snapshot = [
        (pid, ams_id, tray_id, tag_uid, tray_uuid)
        for pid, slots in _respool_prompt_dedup.items()
        for (ams_id, tray_id), (tag_uid, tray_uuid) in slots.items()
    ]

    payloads: list[dict] = []
    for pid, ams_id, tray_id, tag_uid, tray_uuid in snapshot:
        try:
            state = printer_manager.get_status(pid)
            tray = _resolve_live_tray(state, ams_id, tray_id)
            # Only while the SAME tag still physically occupies the slot; a gone /
            # re-tagged slot is stale.
            if not tray or not tray.get("tray_type"):
                continue
            if (tray.get("tag_uid") or "") != tag_uid or (tray.get("tray_uuid") or "") != tray_uuid:
                continue
            donor = await get_spool_by_tag(db, tag_uid, tray_uuid)
            # Durable resolution signals: a re-spool archives/hard-deletes the donor
            # (and clears the dedup); a dismissal stamps respool_dismissed_at without
            # touching the dedup — both must suppress the prompt.
            if donor is None or donor.archived_at is not None or donor.respool_dismissed_at is not None:
                continue
            payloads.append(await _build_respool_prompt_payload(db, pid, ams_id, tray_id, tray, donor))
        except Exception:  # noqa: BLE001 — one slot's failure must not abort the snapshot
            logger.exception("respool_prompt snapshot failed for printer %s AMS%d-T%d", pid, ams_id, tray_id)
    return payloads


async def rebroadcast_unresolved_respool_prompts(db: AsyncSession, send) -> int:
    """Replay every still-unresolved ``respool_prompt`` to a (re)connecting client.

    The ``respool_prompt`` WS event is fire-once — ``ws_manager.broadcast`` reaches
    only sockets connected at emit time and keeps no backlog — so a client that was
    disconnected when a prompt fired never learns of it (F2). This sends the
    re-validated snapshot :func:`pending_respool_prompts` builds to the single ``send``
    coroutine (the reconnecting socket's ``send_json``), bypassing the dedup *guard*
    (which would suppress a re-send) without mutating the dedup state. Returns the
    number re-sent. Never raises (a reconnect must never break on a farm-side hook).
    """
    try:
        payloads = await pending_respool_prompts(db)
    except Exception:  # noqa: BLE001 — a reconnect must never break on the replay hook
        logger.exception("respool_prompt re-broadcast snapshot failed")
        return 0

    sent = 0
    for payload in payloads:
        try:
            await send(payload)
            sent += 1
        except Exception:  # noqa: BLE001 — one slot's failure must not abort the replay
            logger.exception(
                "respool_prompt re-broadcast failed for printer %s AMS%s-T%s",
                payload.get("printer_id"),
                payload.get("ams_id"),
                payload.get("tray_id"),
            )

    if sent:
        logger.info("Re-broadcast %d unresolved respool_prompt(s) to a (re)connecting client", sent)
    return sent


# Minimum gap between the AMS-reported tray remain% and the gram-ledger's implied
# remaining% before a slot is treated as a reused-core refill the ledger missed.
# 30 points is far above ordinary AMS %-quantization noise (integer %, ~10 g steps
# on a 1 kg spool) yet well below the full jump a fresh roll on a spent donor shows
# (production: 958.99/1000 g used → ledger ~4% while the tray read remain=100%).
_RESPOOL_REMAIN_JUMP_PCT = 30.0

# --- Tier-3 evidence gating (2026-07-20 false-popup remediation) -------------
#
# Two false "A reused Bambu tag was detected…" popups reached an operator whose
# farm reuses NO tags. The trigger had fired on the gram ledger alone: donor 45
# read −243 g remaining and donor 34 −813 g (weight_used ABOVE label_weight — an
# impossible state, residue of the over-charge era), and any merely run-down
# seated spool was one AMS push away from the same modal (13 live rows sat ≤50 g).
# Nothing in the gate asked the only question that matters for "did the roll on
# this tag change?": has anybody touched the slot?
#
# * ``_RESPOOL_SWAP_EVIDENCE_S`` — how recently the slot must have seen a QUALIFIED
#   physical presence cycle (``ams_presence.last_physical_cycle_age``) for a swap to
#   be possible at all. That accessor is deliberately non-consuming, so the identify
#   lane and this lane never steal each other's evidence. 10 minutes covers an
#   unhurried roll change and the AMS push that follows it.
# * ``_LEDGER_CORRUPT_TOL_G`` — grams by which weight_used may exceed label_weight
#   before the row is treated as impossible rather than empty. It is a RUNTIME
#   prompt-suppression tolerance only; the DB target is zero negative rows and the
#   repair is the offline tool (``tools/repair/repair_spool_ledger.py``).
# * ``_JUMP_MIN_PUSHES`` / ``_JUMP_STABLE_S`` — a remain jump must hold across at
#   least this many observations spanning this long before it counts, so a single
#   in-flux reading can never prompt.
_RESPOOL_SWAP_EVIDENCE_S = 600.0
_LEDGER_CORRUPT_TOL_G = 50.0
_JUMP_STABLE_S = 10.0
_JUMP_MIN_PUSHES = 2


def _swap_evidence(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Could the roll on this slot physically have been swapped recently?

    True only when ``ams_presence`` recorded a QUALIFIED physical cycle (an
    ABSENT→PRESENT transition past its ≥5 s flap filter) on the slot within
    :data:`_RESPOOL_SWAP_EVIDENCE_S`. This is the whole Tier-3 fix: a near-empty
    spool nobody has touched cannot have become a fresh roll, so it must not raise
    a prompt claiming it might have.

    The accessor is non-consuming, so asking here never robs the identify/discovery
    lane of the same evidence. Fails CLOSED (no evidence → no prompt) and never
    raises — this runs inside the AMS callback chain, per the module's convention
    of local, defensive imports.
    """
    try:
        from backend.app.services import ams_presence

        age = ams_presence.last_physical_cycle_age(printer_id, ams_id, tray_id)
    except Exception:  # noqa: BLE001 — must never break the AMS callback chain
        logger.debug(
            "Swap-evidence lookup failed for printer %s AMS%s-T%s — treating as no evidence",
            printer_id,
            ams_id,
            tray_id,
            exc_info=True,
        )
        return False
    return age is not None and age <= _RESPOOL_SWAP_EVIDENCE_S


def _dismissal_stands(spool: Spool, printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Does the operator's "Same spool" dismissal still hold for this SPENT slot?

    "Same spool" means the physical roll has not changed — so stop reacting until it
    physically does. The dismissal STANDS (and the caller suppresses the whole spent
    branch, auto re-spool AND prompt alike) while ``respool_dismissed_at`` is set and
    NO qualified physical presence cycle has happened on the slot SINCE that answer.
    A qualified cycle strictly AFTER the dismissal re-arms the branch: replacing a
    roll is itself a ≥5 s presence cycle, so genuine exhaustion still surfaces.

    Both spans are measured from now: ``seconds_since_dismissal`` is wall-clock
    (``respool_dismissed_at`` is naive-UTC via ``datetime.utcnow()``) and the cycle
    ``age`` from :func:`ams_presence.last_physical_cycle_age` is monotonic — both
    count elapsed seconds at the same rate, so a cycle whose age is SHORTER than the
    since-dismissal span happened after the dismissal. A ``None`` age (no cycle known
    — the state after every restart, when the in-memory ledger is empty) keeps the
    dismissal standing: a real post-restart swap records a fresh cycle live and
    re-arms then.

    Non-consuming, defensive local import (same convention as :func:`_swap_evidence`);
    never raises — a lookup failure fails quiet to "dismissal stands".
    """
    if spool.respool_dismissed_at is None:
        return False
    seconds_since_dismissal = max(0.0, (datetime.utcnow() - spool.respool_dismissed_at).total_seconds())
    try:
        from backend.app.services import ams_presence

        age = ams_presence.last_physical_cycle_age(printer_id, ams_id, tray_id)
    except Exception:  # noqa: BLE001 — must never break the AMS callback chain
        logger.debug(
            "Dismissal-stands cycle lookup failed for printer %s AMS%s-T%s — treating the dismissal as standing",
            printer_id,
            ams_id,
            tray_id,
            exc_info=True,
        )
        return True
    if age is None:
        return True
    return age >= seconds_since_dismissal


def _ledger_corrupt(spool: Spool) -> bool:
    """Is this row's gram ledger physically impossible?

    ``weight_used`` above ``label_weight`` (beyond :data:`_LEDGER_CORRUPT_TOL_G`)
    computes a NEGATIVE remaining, which the old near-empty test happily read as
    "almost empty" — the direct cause of the false reused-tag popups. A NULL/0
    label with grams charged against it is the same defect (remaining computes
    negative), so it classifies the same way.
    """
    label = spool.label_weight or 0
    used = spool.weight_used or 0
    if label <= 0:
        return used > 0
    return (used - label) > _LEDGER_CORRUPT_TOL_G


def _remain_reading_untrustworthy(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """True while the tray's ``remain`` reading cannot be trusted for corroboration.

    A commanded identify in flight (the value is mid-re-read) or a drying unit (trays
    disengage and re-report) both produce transient tray payloads. Fails CLOSED
    (unknown → untrustworthy → no jump) and never raises, same contract as
    :func:`_swap_evidence`.
    """
    try:
        from backend.app.services import ams_presence

        return ams_presence.identify_in_flight(printer_id, ams_id, tray_id) or ams_presence.unit_drying(
            printer_id, ams_id
        )
    except Exception:  # noqa: BLE001 — must never break the AMS callback chain
        logger.debug(
            "Remain-reading trust check failed for printer %s AMS%s-T%s — treating as untrustworthy",
            printer_id,
            ams_id,
            tray_id,
            exc_info=True,
        )
        return True


def _remain_jump_reading(spool: Spool, tray: dict) -> bool:
    """Detect a reused-core refill the gram ledger cannot see.

    A reused Bambu core carries its RFID tag onto a FRESH roll, so the firmware
    re-reads the tray as ~full (``remain`` ≈ 100%) while our ledger still holds the
    donor's near-spent ``weight_used``. The tag identity is CORRECT, so RFID
    re-reads never fix it — only a re-spool resets the ledger. True iff the tray
    carries a valid tag, the spool has a positive label weight, the tray ``remain``
    parses to an int in 1..100, and it exceeds the ledger's implied remaining % by
    at least :data:`_RESPOOL_REMAIN_JUMP_PCT`. A weight-locked fresh row (ledger
    ≈100%) cannot jump — ``remain`` cannot exceed 100 by 30 — so no special-case is
    needed for it.

    This is the INSTANTANEOUS reading only: pure arithmetic over one tray payload,
    no state, no trust check. The push-driven trigger consumes the corroborated
    :func:`_remain_jump` instead; the operator-initiated dismissal route (a single
    deliberate question about the live tray, with no push history to corroborate
    against) consumes this one.
    """
    if not is_valid_tag(tray.get("tag_uid", "") or "", tray.get("tray_uuid", "") or ""):
        return False
    label_weight = spool.label_weight or 0
    if label_weight <= 0:
        return False
    try:
        remain = int(tray.get("remain"))
    except (TypeError, ValueError):
        return False
    if not (1 <= remain <= 100):
        return False
    ledger_pct = max(0, label_weight - (spool.weight_used or 0)) / label_weight * 100
    return (remain - ledger_pct) >= _RESPOOL_REMAIN_JUMP_PCT


def _remain_jump(spool: Spool, tray: dict, printer_id: int, ams_id: int, tray_id: int) -> bool:
    """A remain jump CORROBORATED across pushes — the push-driven trigger's test.

    One push is not evidence: the AMS re-reports a tray on every state change, and a
    reading taken mid-identify or mid-drying is in flux. So the instantaneous
    :func:`_remain_jump_reading` only starts a corroboration window here, and the
    jump counts only once it has been observed on ≥ :data:`_JUMP_MIN_PUSHES`
    pushes spanning ≥ :data:`_JUMP_STABLE_S` — a genuine refilled core keeps reading
    the same way, an artefact does not. A push that stops reading as a jump drops the
    window entirely (the condition must HOLD, not merely have happened once), and an
    untrustworthy push neither fires nor counts.

    Stateful by necessity (:data:`_jump_seen`), so it is the trigger path's helper;
    anything wanting the pure arithmetic calls :func:`_remain_jump_reading`.
    """
    key = (printer_id, ams_id, tray_id)
    if not _remain_jump_reading(spool, tray):
        _jump_seen.pop(key, None)
        return False
    if _remain_reading_untrustworthy(printer_id, ams_id, tray_id):
        return False
    now = _monotonic()
    first_seen, count = _jump_seen.get(key, (now, 0))
    count += 1
    _jump_seen[key] = (first_seen, count)
    return count >= _JUMP_MIN_PUSHES and (now - first_seen) >= _JUMP_STABLE_S


def should_evaluate_respool(spool: Spool, tray: dict, printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Single-origin gate for the existing-assignment respool call site.

    True when :func:`maybe_auto_or_prompt_respool` should run for a slot whose
    ``SpoolAssignment`` survived: either the spool is hardware-spent (Tier 1/2) or
    the tray shows a CORROBORATED remain-jump refill the gram ledger missed (a Tier 3
    trigger). Keeps the jump logic out of ``main.on_ams_change`` so there is one
    definition; the slot coordinates are what let the jump corroborate per slot.
    """
    return spool.spent_at is not None or _remain_jump(spool, tray, printer_id, ams_id, tray_id)


async def maybe_auto_or_prompt_respool(
    db: AsyncSession,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tray: dict,
    spool: Spool,
) -> Spool | None:
    """Tier 2/3 gate for a tag arrival that resolved to inventory ``spool``.

    * Dismissal gate (spent branch): when ``respool_dismissed_at`` is set and no
      qualified physical cycle has happened on the slot since the answer
      (:func:`_dismissal_stands`), the whole spent branch — Tier-2 auto AND prompt —
      is suppressed. A qualified roll swap after the answer re-arms it.
    * Tier 2 (auto): ``spool.spent_at`` set AND the tray is LOADED → the physical
      spool cannot be the spent one, so re-spool with the server-held last brand
      and return the NEW spool (the caller must skip its own auto-assign — the
      re-spool already re-assigned the slot). Empty brand or a sibling conflict
      falls through to the prompt instead.
    * Tier 3 (prompt): ``spent_at`` NULL, the ledger is plausible, and (remaining ≤
      threshold OR a corroborated remain-jump refill the ledger missed) AND the slot
      shows recent physical evidence a roll could have changed
      (:func:`_swap_evidence`) → broadcast a deduped ``respool_prompt`` and return
      None (existing auto-assign proceeds). An impossible ledger row logs a WARNING
      and prompts nothing.
    * Otherwise: None (no-op).

    No-op in Spoolman mode.
    """
    if await _spoolman_enabled(db):
        return None

    if spool.spent_at is not None:
        if not _tray_loaded(tray):
            return None  # spent but not loaded → dead spool re-inserted, no trigger
        if _dismissal_stands(spool, printer_id, ams_id, tray_id):
            # "Same spool" was answered and the physical roll has not changed since
            # (no qualified presence cycle after the dismissal), so suppress the ENTIRE
            # spent branch — auto re-spool AND prompt. This is what stops a standing
            # FALSE spent stamp from re-firing forever; with auto enabled it also stops
            # every tag re-read from minting a phantom fresh row. A qualified swap after
            # the answer re-arms the branch, so a genuine later exhaustion still surfaces.
            return None
        if not await _respool_auto_enabled(db):
            # Tier-2 auto re-spool disabled (default): a spent+loaded tag arrival
            # surfaces the one-click prompt instead of silently minting a fresh row,
            # so a false spent stamp can never auto-corrupt the ledger — the operator
            # confirms the physical roll before the tag moves onto a fresh spool.
            await _broadcast_respool_prompt(db, printer_id, ams_id, tray_id, tray, spool)
            return None
        brand = (await _respool_last_brand(db)).strip()
        if not brand:
            # 3b-5: before the first-ever manual re-spool the server-held last
            # brand is empty. Fall back to the configured tagless-default brand
            # (ONE source of truth — the spool_tagless parser; local import per
            # this module's cycle-avoidance convention) so a hardware-certain
            # spent+loaded spool still auto-respools instead of prompting. Never
            # invents a brand: the accessor returns "" when the setting is off,
            # keeping today's prompt fallback below.
            from backend.app.services.spool_tagless import tagless_default_brand

            brand = await tagless_default_brand(db)
        if not brand:
            # No prefill brand and no configured default → can't auto safely;
            # surface the one-click prompt.
            await _broadcast_respool_prompt(db, printer_id, ams_id, tray_id, tray, spool)
            return None
        try:
            new_spool = await respool_tag(
                db,
                printer_id=printer_id,
                ams_id=ams_id,
                tray_id=tray_id,
                brand=brand,
            )
            logger.info(
                "Auto re-spooled tag on printer %d AMS%d-T%d: donor #%d → spool #%d",
                printer_id,
                ams_id,
                tray_id,
                spool.id,
                new_spool.id,
            )
            return new_spool
        except RespoolSiblingConflict as exc:
            logger.warning(
                "Auto re-spool skipped on printer %d AMS%d-T%d (sibling-tag conflict): %s",
                printer_id,
                ams_id,
                tray_id,
                exc.detail,
            )
            await _broadcast_respool_prompt(db, printer_id, ams_id, tray_id, tray, spool)
            return None
        except RespoolError as exc:
            logger.warning(
                "Auto re-spool failed on printer %d AMS%d-T%d: %s",
                printer_id,
                ams_id,
                tray_id,
                exc.detail,
            )
            return None

    # Tier 3: uncertain — spent_at NULL. Two gates, in order.
    #
    # (1) An IMPOSSIBLE ledger row is reported, never prompted. weight_used above
    # label_weight computes a negative remaining, which the pre-2026-07-20 trigger
    # read as "almost empty" and turned into a modal announcing a reused tag on a
    # farm that reuses none (production donors 45 at −243 g and 34 at −813 g). The
    # data is repaired by the offline tool, not at runtime: no auto-correction, no
    # health flag, no new event — deliberately one WARNING and out (operator
    # decision 2026-07-20), so the row stays visible until it is actually fixed.
    if _ledger_corrupt(spool):
        logger.warning(
            "Impossible spool ledger — re-spool prompt suppressed: spool %d on printer %d AMS%d-T%d "
            "(label %.1f g, used %.1f g → remaining %.1f g). weight_used exceeds the label, so "
            "'near-empty' is meaningless here. Repair the row with tools/repair/repair_spool_ledger.py.",
            spool.id,
            printer_id,
            ams_id,
            tray_id,
            float(spool.label_weight or 0),
            float(spool.weight_used or 0),
            float((spool.label_weight or 0) - (spool.weight_used or 0)),
        )
        return None
    # Suppress once the operator answered "Same spool" (respool_dismissed_at
    # stamped): a deliberately-run-down near-empty spool must not re-prompt on every
    # reseat / AMS power-cycle / server restart (the in-memory dedup cannot survive
    # those). Tier-3 suppression here is PERMANENT for the row — a non-spent
    # near-empty spool only becomes interesting again once it is actually re-spooled
    # (which clears the row). The spent branch ABOVE reads the SAME dismissal
    # differently: there it holds only per physical cycle (:func:`_dismissal_stands`),
    # re-arming on a qualified roll swap after the answer, because a genuine hardware
    # exhaustion must still surface.
    if spool.respool_dismissed_at is not None:
        return None
    # (2) A prompt needs a REASON and EVIDENCE. The reason is the ledger reading
    # near-empty or a corroborated remain-jump (a reused core carried the tag onto a
    # fresh roll and the gram ledger never noticed). The evidence is physical: unless
    # somebody actually cycled a roll through this slot recently, the spool in it is
    # the same one the ledger already describes and there is nothing to ask about.
    # This is what silences the standing near-empty rows — they are near-empty
    # because they were printed down, not because a roll was swapped.
    remaining = (spool.label_weight or 0) - (spool.weight_used or 0)
    threshold = await _respool_prompt_threshold_g(db)
    if not (remaining <= threshold or _remain_jump(spool, tray, printer_id, ams_id, tray_id)):
        return None
    if not _swap_evidence(printer_id, ams_id, tray_id):
        logger.debug(
            "Re-spool prompt withheld for spool %d (printer %d AMS%d-T%d): no physical roll cycle "
            "on the slot within %.0fs — an untouched spool cannot have become a fresh roll",
            spool.id,
            printer_id,
            ams_id,
            tray_id,
            _RESPOOL_SWAP_EVIDENCE_S,
        )
        return None
    await _broadcast_respool_prompt(db, printer_id, ams_id, tray_id, tray, spool)
    return None


# --- Core operation ---------------------------------------------------------


async def respool_tag(
    db: AsyncSession,
    *,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    brand: str,
    label_weight: int | None = None,
    cost_per_kg: float | None = None,
    note: str | None = None,
) -> Spool:
    """Re-spool a reused Bambu tag onto a fresh full third-party spool.

    Resolves the live tray, guards against a sibling-tag merge, disposes the
    donor (hard-delete a pristine drive-by auto-create, else archive), mints a
    fresh full spool (weight_used=0, weight_locked, spent_at NULL,
    tag_type=bambulab_reused), copies the donor's K-profiles, re-assigns the AMS
    slot, updates the last-brand prefill, commits, and releases low-spool-staged
    farm items. Broadcasts ``spool_respooled``.

    Raises :class:`RespoolError` (404 not connected / 400 empty-or-no-tag) or
    :class:`RespoolSiblingConflict` (409) — the caller maps these to HTTP or the
    prompt fallback.
    """
    from backend.app.services.printer_manager import printer_manager

    # 1. Resolve the live tray + tag identity.
    state = printer_manager.get_status(printer_id)
    if not state or not getattr(state, "raw_data", None):
        raise RespoolError(404, "Printer not connected or no live state available")
    tray = _resolve_live_tray(state, ams_id, tray_id)
    if not tray or not tray.get("tray_type"):
        raise RespoolError(400, "Slot is empty or has no readable tray data")
    scan_tag_uid = tray.get("tag_uid", "")
    scan_tray_uuid = tray.get("tray_uuid", "")
    if not is_valid_tag(scan_tag_uid, scan_tray_uuid):
        raise RespoolError(400, "Slot has no valid RFID tag")
    norm_uid = normalize_tag_uid(scan_tag_uid)
    norm_uuid = normalize_tray_uuid(scan_tray_uuid)

    # 2. Sibling-tag guard + donor resolution. get_spool_by_tag prefers tray_uuid,
    # so a tray_uuid match that is itself a reused-type row with a DIFFERENT
    # tag_uid is the donor's sibling already living on another spool → refuse.
    donor = await get_spool_by_tag(db, scan_tag_uid, scan_tray_uuid)
    if (
        donor is not None
        and donor.tag_type == RESPOOL_TAG_TYPE
        and norm_uuid
        and normalize_tray_uuid(donor.tray_uuid or "") == norm_uuid
    ):
        donor_uid = normalize_tag_uid(donor.tag_uid or "")
        if norm_uid and donor_uid and donor_uid != norm_uid:
            raise RespoolSiblingConflict(donor.id)

    # Idempotency: the resolved row is ALREADY the fresh re-spooled record for
    # this very tag (double-submit, or the auto path racing a manual confirm) —
    # disposing it and minting another would churn a duplicate. Return it
    # unchanged; a brand correction goes through the normal spool edit.
    if (
        donor is not None
        and donor.tag_type == RESPOOL_TAG_TYPE
        and donor.spent_at is None
        and not (donor.weight_used or 0)
        and norm_uid
        and normalize_tag_uid(donor.tag_uid or "") == norm_uid
    ):
        logger.info("Re-spool no-op: spool %d is already the fresh record for tag %s", donor.id, norm_uid)
        return donor

    # Capture everything needed from the donor BEFORE disposal (a pristine donor
    # is hard-deleted, which cascade-removes its K-profiles).
    donor_id: int | None = donor.id if donor else None
    donor_kprofiles: list[dict] = []
    donor_fields: dict = {}
    if donor is not None:
        donor_fields = {
            "material": donor.material,
            "subtype": donor.subtype,
            "color_name": donor.color_name,
            "rgba": donor.rgba,
            "extra_colors": donor.extra_colors,
            "effect_type": donor.effect_type,
            "core_weight": donor.core_weight,
            "core_weight_catalog_id": donor.core_weight_catalog_id,
            "slicer_filament": donor.slicer_filament,
            "slicer_filament_name": donor.slicer_filament_name,
            "nozzle_temp_min": donor.nozzle_temp_min,
            "nozzle_temp_max": donor.nozzle_temp_max,
            "label_weight": donor.label_weight,
        }
        for kp in donor.k_profiles:
            donor_kprofiles.append(
                {
                    "printer_id": kp.printer_id,
                    "extruder": kp.extruder,
                    "nozzle_diameter": kp.nozzle_diameter,
                    "nozzle_type": kp.nozzle_type,
                    "k_value": kp.k_value,
                    "name": kp.name,
                    "cali_idx": kp.cali_idx,
                    "setting_id": kp.setting_id,
                }
            )

    # 3. Dispose the donor: strip tags, drop its slot assignments, then
    # hard-delete a pristine drive-by auto-create or archive a ledger-bearing row.
    if donor is not None:
        donor.tag_uid = None
        donor.tray_uuid = None
        for assignment in list(donor.assignments):
            await db.delete(assignment)
        await db.flush()

        history_count = await db.scalar(
            select(func.count(SpoolUsageHistory.id)).where(SpoolUsageHistory.spool_id == donor.id)
        )
        if donor.data_origin == "rfid_auto" and not history_count:
            await db.delete(donor)  # pristine auto-create — no ledger to preserve
            disposition = "hard-deleted"
        else:
            donor.archived_at = datetime.utcnow()
            disposition = "archived"
        await db.flush()
    else:
        disposition = "none"

    # 4. Mint the fresh full third-party spool. Identity from the donor when we
    # have it, else parsed straight from the tray (shared helper).
    if donor_fields:
        source = donor_fields
    else:
        parsed = await parse_tray_fields(db, tray)
        source = {
            "material": parsed.material,
            "subtype": parsed.subtype,
            "color_name": parsed.color_name,
            "rgba": parsed.rgba,
            "extra_colors": None,
            "effect_type": None,
            "core_weight": parsed.core_weight,
            "core_weight_catalog_id": None,
            "slicer_filament": parsed.slicer_filament,
            "slicer_filament_name": parsed.slicer_filament_name,
            "nozzle_temp_min": parsed.nozzle_temp_min,
            "nozzle_temp_max": parsed.nozzle_temp_max,
            "label_weight": parsed.label_weight,
        }

    final_label_weight = int(label_weight) if label_weight else int(source["label_weight"] or 1000)

    new_spool = Spool(
        material=source["material"],
        subtype=source["subtype"],
        color_name=source["color_name"],
        rgba=source["rgba"],
        extra_colors=source["extra_colors"],
        effect_type=source["effect_type"],
        brand=brand,
        label_weight=final_label_weight,
        core_weight=source["core_weight"] or 250,
        core_weight_catalog_id=source["core_weight_catalog_id"],
        weight_used=0,  # fresh full spool by definition
        weight_locked=True,  # neutralize the donor tag's stale AMS remain%
        spent_at=None,
        slicer_filament=source["slicer_filament"],
        slicer_filament_name=source["slicer_filament_name"],
        nozzle_temp_min=source["nozzle_temp_min"],
        nozzle_temp_max=source["nozzle_temp_max"],
        tag_uid=norm_uid if norm_uid and norm_uid != ZERO_TAG_UID else None,
        tray_uuid=norm_uuid if norm_uuid and norm_uuid != ZERO_TRAY_UUID else None,
        data_origin="rfid_linked",
        tag_type=RESPOOL_TAG_TYPE,
        cost_per_kg=cost_per_kg,
        note=note,
    )
    # Initialize relationships before add() to avoid a lazy load in async context
    # (SpoolAssignment back_populates resolution runs synchronously — see #612).
    new_spool.k_profiles = []
    new_spool.assignments = []
    db.add(new_spool)
    await db.flush()

    # 5. Copy donor K-profiles (same-performance filament per the operator).
    for kp in donor_kprofiles:
        db.add(SpoolKProfile(spool_id=new_spool.id, **kp))
    await db.flush()

    # 6. Assign the slot + re-apply K-profile via MQTT.
    await auto_assign_spool(
        printer_id,
        ams_id,
        tray_id,
        new_spool,
        printer_manager,
        db,
        tray_info_idx=tray.get("tray_info_idx", ""),
    )

    # 7. Persist the last-brand prefill and commit the atomic unit (3-6).
    from backend.app.api.routes.settings import set_setting

    await set_setting(db, "respool_last_brand", brand)
    await db.commit()
    logger.info(
        "Re-spooled tag on printer %d AMS%d-T%d: donor %s (%s) → fresh spool %d (%s, %dg, locked)",
        printer_id,
        ams_id,
        tray_id,
        donor_id if donor_id is not None else "none",
        disposition,
        new_spool.id,
        brand,
        final_label_weight,
    )

    await ws_manager.broadcast(
        {
            "type": "spool_respooled",
            "printer_id": printer_id,
            "ams_id": ams_id,
            "tray_id": tray_id,
            "donor_spool_id": donor_id,
            "new_spool_id": new_spool.id,
            "brand": brand,
            "label_weight": final_label_weight,
        }
    )

    # 8. Release low-spool-staged farm units without waiting for an AMS push
    # (commits internally, per-item fail-safe).
    from backend.app.services.farm_staging import release_filament_staged

    await release_filament_staged(db, printer_id)

    # The dedup for this slot is stale now the tag maps to a fresh spool.
    clear_respool_prompt_dedup(printer_id, ams_id, tray_id)
    return new_spool
