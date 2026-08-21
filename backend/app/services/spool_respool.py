"""Reused Bambu RFID tag → spent-certain auto re-spool (fork farm feature).

The farm refills spent Bambu 1 kg rolls by peeling the RFID tag onto a fresh
third-party spool. The AMS then auto-identifies the filament, but Bambuddy's
spool ledger would otherwise map the tag to the SPENT donor row (weight_used ≈
1000 g), silently stalling the lights-out queue on the #1496 filament-deficit
guard. This module is the single owner of the re-spool operation and its three
certainty tiers:

* **Tier 1 — spent-certain marking** (`apply_runout_edges` → `mark_spent_on_runout` /
  `mark_spent_on_slot_runout`, `mark_spent_on_runout_hold`, and `sample_status_push` +
  `confirm_backup_swaps`): a hardware runout signal (unrescued runout HMS / the
  firmware's own per-event slot-attributed runout words / a held-runout escalation /
  seamless AMS backup-swap) stamps ``spool.spent_at`` — the certainty key. Never set by
  gram estimates. The two HMS lanes fire on a wire-HMS APPEARANCE EDGE
  (``services.hms_edges``): "NEW" is that tracker's answer, never the notification
  dedup's 600 s re-notify window, which is a level-triggered alert policy and the wrong
  axis for a state decision. Because those edges are re-seeded by every restart, the
  HELD class has a second, DURABLE entry point that rides the recovery incident's
  escalation instead of a wire edge (`mark_spent_on_runout_hold`). The backup-swap
  detector is a SAMPLER/CONFIRMER pair, not one call: the sync sampler runs on every
  status push (~1 Hz) because the tray_now edge it watches is invisible to the
  AMS-hash-gated callback, and only a confirmed departure pays for a DB session.
* **Tier 2 — automatic re-spool** (`maybe_auto_or_prompt_respool`): a tag arrival
  resolving to a FINISHED roll (`Spool.is_finished_roll`) on a LOADED tray physically
  cannot be the old (empty) spool, so it re-spools with no operator involvement —
  unless a standing "Same spool" dismissal still holds for the slot (see below). It
  CONCLUDES; it never asks (operator ruling 3, 2026-08-19 — the `respool_auto_enabled`
  toggle that used to make it ask is deleted, not defaulted on).
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
standing false spent stamp stops re-reacting — and stops minting phantom fresh rows —
the moment it is dismissed. That dismissal is now the ONLY brake in front of tier 2,
which is why it is checked before the conclusion rather than after it.

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
from backend.app.models.printer import Printer
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_k_profile import SpoolKProfile
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import spool_binding
from backend.app.services.hms_errors import RUNOUT_HMS_CODES, hms_short_code
from backend.app.services.spool_tag_matcher import (
    ZERO_TAG_UID,
    ZERO_TRAY_UUID,
    auto_assign_spool,
    get_spool_by_tag,
    is_valid_tag,
    parse_tray_fields,
)
from backend.app.services.tray_fields import (
    normalized_tag_uid,
    normalized_tray_uuid,
    parse_filam_bak,
    tray_presence_from_dict,
)
from backend.app.utils.tag_normalization import normalize_tag_uid, normalize_tray_uuid, tag_matches_row

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Tag vendor marker written on every re-spooled row. Single origin of truth so
# the sibling-tag guard and the observability hook agree on the classification.
RESPOOL_TAG_TYPE = "bambulab_reused"

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

# How far a roll's DELIVERED grams may fall short of its assumed label before the spent
# stamp bothers to record the gap. This used to be a WARNING floor, on the reading that a
# fat remainder at runout signalled drift or an initial-state error. Operator ruling 18
# (2026-08-19) retired that reading: a minted row's label weight is an ASSUMPTION, so a
# part-used roll an operator seated legitimately delivers ~800 g on a 1000 g label, and
# some brands ship ~1100 g on the same label. Both are ordinary stock. The gap is
# therefore RECORDED, not warned about — the hardware runout is what closes the roll's
# true capacity (``Spool.delivered_g``), and 14 rows carrying 843/580/576/453/418/417 g
# "remaining" at their stamp were every one of them a part-used roll minted as full.
_SPENT_DELIVERY_GAP_NOTE_G = 150.0

# How long after a slot's bay cleared the exhaustion evidence FOR THAT CLEAR may still
# arrive — the scope of one runout episode, used by :func:`_bound_after_the_bay_cleared`
# and nowhere else.
#
# The AMS clears a drained slot's exist bit ~3 MINUTES BEFORE it declares the runout (the
# tail is still traversing the feed path; three timed pairs on 2026-08-13 — 03:55:46 →
# 03:58:20, 06:41:47 → 06:44:44, 07:02:52 → 07:05:33). A release and the HMS that explains
# it are therefore two halves of ONE physical episode, separated by a firmware delay, and a
# slot accumulates many such clears over its life. Deciding whether the roll bound RIGHT NOW
# could be the roll that drained needs to know WHICH clear the arriving evidence is about,
# and the gap is the only thing that says so.
#
# This is not identity evidence and it concludes nothing on its own (doctrine rules 6/7 —
# duration never decides identity POSITIVELY). It decides one NEGATIVE: outside the window
# the farm asserts nothing new and tier 1 keeps its incumbent, which is the ordinary and
# overwhelmingly common answer (a slot's previous occupant left hours or days ago).
#
# 600 s is ~3.4x the measured maximum gap: wide enough to absorb firmware variance, a
# queued push and a restart landing inside the episode, and far short of the "operator
# refilled and the slot has been printing for an hour" shape, which must never disqualify
# tier 1. Both mis-sizings cost a FALSE stamp, in opposite directions — too narrow stamps
# the operator's brand-new roll (the shipped regression this constant exists to close), too
# wide stamps a healthy roll pulled from the slot shortly before a genuinely different
# roll's runout. The measured physics is the only defensible anchor, so the value is tied to
# it rather than to a round number that felt safe.
_BAY_CLEAR_TO_RUNOUT_GAP_S = 600.0

# Seconds a tray_now value must hold unchanged during RUNNING to count as the stable
# feeder, and for a pending backup swap to confirm into a spent stamp.
_SWAP_CONFIRM_S = 60.0
# A commanded-load marker older than this is stale (the load never took / a much
# later unrelated edge); it stops suppressing.
_COMMANDED_LOAD_TTL_S = 600

# Per-printer dedup for `respool_prompt` WS broadcasts, keyed
# (ams_id, tray_id) -> (tag_uid, tray_uuid). Mirrors slot_pipeline's unknown-tag dedup:
# re-broadcast only when the tag tuple changes for the slot; cleared when the
# slot goes empty so remove + reinsert re-prompts.
_respool_prompt_dedup: dict[int, dict[tuple[int, int], tuple[str, str]]] = {}

# Tier-3 OBSERVATION dedup (2026-08-10 demotion), keyed identically. Kept separate
# from `_respool_prompt_dedup` on purpose: that one is the record of what the
# operator was ASKED — `pending_respool_prompts` replays it to reconnecting clients
# and `respool_prompt_open_for_slot` reads it — while a tier-3 observation asks
# nobody anything. Same lifetime rules (cleared on the slot-empty edge and by
# `_reset_state`), so a genuine roll swap re-arms the line.
_respool_observation_logged: dict[int, dict[tuple[int, int], tuple[str, str]]] = {}

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


def _reset_state() -> None:
    """Test hook: clear module-level edge/dedup state between cases."""
    global _last_contradiction_scan_at

    _last_tray_now.clear()
    _stable_feeder.clear()
    _feeder_since.clear()
    _pending_swaps.clear()
    _commanded_loads.clear()
    _last_sample_job.clear()
    _respool_prompt_dedup.clear()
    _respool_observation_logged.clear()
    _jump_seen.clear()
    _spent_dedup.clear()
    _filam_bak_groups.clear()
    _last_contradiction_scan_at = None


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


def decode_global_tray(global_tray: int | None) -> tuple[int | None, int | None]:
    """Decode a global tray id to (ams_id, tray_id) for SpoolAssignment lookup.

    THE fork's global-tray codec, with :func:`encode_global_tray` as its exact inverse
    (cross-cutting invariant 1 — one origin per magic value). Three conventions, only one
    of which is the obvious arithmetic:

    * regular AMS — ``global = ams_id * 4 + slot`` (0..127);
    * AMS-HT (128..191) — a single-tray unit reports ``global == ams_id``, so the ``* 4``
      arithmetic is simply WRONG for it;
    * external vt_tray 254/255 → ``ams_id = 255``, slot 0/1 (the ``tray_id + 254``
      convention from the auto-unlink path).

    A bare ``ams_id * 4 + tray_id`` anywhere in the fork silently drops the last two, which
    is why the codec is public and the arithmetic is not to be re-spelled at call sites.
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


def encode_global_tray(ams_id: int | None, tray_id: int | None) -> int | None:
    """Encode ``(ams_id, tray_id)`` to a global tray id — the inverse of the decoder above.

    Every convention the decoder knows, applied in the same order, so a round trip through
    the pair is the identity for every slot the fork can name. ``None`` for an unaddressable
    slot (either half missing, a negative, or an AMS unit outside the layout), because a
    fabricated global id would compare EQUAL to some real slot and quietly mis-attribute a
    fault to it — fail closed, exactly as the decoder does.
    """
    if ams_id is None or tray_id is None:
        return None
    try:
        unit, slot = int(ams_id), int(tray_id)
    except (TypeError, ValueError):
        return None
    if unit < 0 or slot < 0:
        return None
    if unit == 255:  # the external vt_tray holder: 254/255
        return 254 + slot if slot <= 1 else None
    if 128 <= unit <= 191:  # AMS-HT — single tray, the unit id IS the global id
        return unit if slot == 0 else None
    if unit <= 31 and slot <= 3:
        return unit * 4 + slot
    return None


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


def _last_clear_of(spool: Spool | None, *, printer_id: int, ams_id: int, tray_id: int) -> datetime | None:
    """When ``spool``'s last release was FROM this exact slot, or None.

    A column read on a row already in hand, not a second answer to
    ``last_released_from_slot_stmt``'s question: that stmt asks which row last left a slot
    (a single candidate, adjudicated by its caller). This asks something the stmt cannot:
    whether the row the caller is holding recorded its own departure from this slot before
    returning to it.
    """
    if spool is None:
        return None
    here = (spool.last_location_printer_id, spool.last_location_ams_id, spool.last_location_tray_id)
    if here != (printer_id, ams_id, tray_id):
        return None
    return spool.last_location_at


def _bound_after_the_bay_cleared(
    assignment: SpoolAssignment | None,
    residue: Spool | None,
    *,
    printer_id: int,
    ams_id: int,
    tray_id: int,
) -> bool:
    """Did the roll bound to this slot arrive AFTER the bay-clear this runout explains?

    Tier 1's ELIGIBILITY test, and a physical impossibility rather than a heuristic: a roll
    that was bound to the slot only after the bay emptied cannot be the roll that emptied it.

    It exists because the 2026-08-19 spool-identity wave changed a precondition this lane
    depends on without re-checking the lane (the shape memory ``liveness-paired-verification``
    is about — the same way the 08-13 wave starved every spent stamp for three days). That
    wave made a refill inside the ~3-minute bay-clear→HMS gap MINT a fresh row and bind it
    (scenarios T7/T8 — "a runout release is never a glitch"), which is correct. But tier 1
    resolves its victim from the LIVE :class:`SpoolAssignment`, so the runout HMS arriving
    two minutes later stamped whatever was bound: probe result ``FRESH spent_at:
    2026-08-20 10:29:14 used: 0.0`` / ``DRAINED spent_at: None used: 900.0``. The brand-new
    roll then reads 0 g remaining (rule 8 derives emptiness from ``spent_at``), is
    hard-excluded from selection with no automatic un-spend lane — the slot is dead until a
    human intervenes — while the roll that actually ran dry stays un-spent and remains a
    selection and reclaim candidate.

    Two facts decide it, both already durable, neither of them new state:

    * ``SpoolAssignment.created_at`` — the bind moment. The same wave made it meaningful:
      a genuine MINT stamps a fresh one, while a DE-BOUNCE deliberately carries the
      incumbent's ORIGINAL moment forward (``bind_spool_to_slot(..., bind_moment=...)``,
      ``slot_pipeline._debounce_bind_moment``). That is precisely what keeps a de-bounced
      roll ELIGIBLE here: its binding began before the spurious release, so it is the same
      seating and the same roll — scenario T8b, where tier 1 stamping the incumbent is the
      right answer.
    * ``Spool.last_location_at`` on the residue — when the departing roll's release stamped
      its breadcrumb, i.e. when the bay cleared.

    Ordering alone cannot decide this, and that is the whole subtlety. In the ordinary case
    (T6) the incumbent ALSO bound after the slot's previous occupant left — every roll does.
    What separates "bound after the clear this runout is about" from "bound after some clear
    weeks of prod ago" is :data:`_BAY_CLEAR_TO_RUNOUT_GAP_S`, the measured firmware delay
    between the two halves of one runout episode. So both conditions are required: the
    ordering states the impossibility, the window says which clear is being talked about.

    Three exits keep tier 1, and each is a distinct reason, not a defensive default:

    * **No residue.** Tier 1 stands aside only in favour of a NAMED alternative. With
      nothing for tier 2 to resolve, disqualifying the incumbent buys no correctness and
      costs a stamp — and the shape is real (a glitched exist bit releases a TAGGED roll,
      whose next read re-binds it with a fresh moment).
    * **The incumbent vacated this bay more recently than the residue did**
      (:func:`_last_clear_of`). Then the residue's departure predates the incumbent's whole
      tenure here, so it cannot be the roll that just drained; the only candidates are the
      incumbent and nothing, and only the incumbent has evidence.
    * **The binding predates the clear.** Either this IS the drained roll (its own release
      has not happened, and on a bits-less dialect never will), or a de-bounce carried its
      original moment across a spurious one, or it simply DISPLACED the previous occupant —
      a bay that never emptied.

    **Accepted residual, stated rather than hidden:** two departures from one slot inside
    the window, where the more recent departure's row has since re-bound with a FRESH moment
    (a tagged roll re-read after a glitch release, which does not preserve a bind moment),
    resolves the clear to that row's own departure and keeps tier 1 — correct — but the
    mirror shape, where the returning row's breadcrumb has been overwritten elsewhere, would
    attribute to the older residue. It needs two roll departures from one slot inside ten
    minutes, and the alternative (no window at all) is the shipped regression.
    """
    if assignment is None or residue is None:
        return False
    bound_at = getattr(assignment, "created_at", None)
    cleared_at = getattr(residue, "last_location_at", None)
    if bound_at is None or cleared_at is None:
        return False
    own_clear = _last_clear_of(
        getattr(assignment, "spool", None), printer_id=printer_id, ams_id=ams_id, tray_id=tray_id
    )
    if own_clear is not None and own_clear >= cleared_at:
        return False
    if bound_at <= cleared_at:
        return False
    return (datetime.utcnow() - cleared_at).total_seconds() <= _BAY_CLEAR_TO_RUNOUT_GAP_S


async def _newest_released_from_slot(db: AsyncSession, printer_id: int, ams_id: int, tray_id: int) -> Spool | None:
    """The ONE row this slot last released, or None — read through the shared origin.

    ``spool_binding.last_released_from_slot_stmt`` is that origin (writer and reader in one
    module); this is the spent lane's single call site for it, so the eligibility test and
    tier 2 provably adjudicate the SAME row rather than two queries that could drift apart.

    ``.limit(1)`` and no filters: the newest row is adjudicated wherever it leads (already
    spent → idempotent, archived → stand down). A ``spent_at IS NULL`` filter here would
    walk PAST the victim onto an older, healthy residue of the same slot and stamp it
    permanently — cross-cutting invariant 11 applied to the query shape.

    The ONE adjudication made here rather than by :func:`_mark_tray_spent` is BOUND
    ELSEWHERE, and it is made here because it answers the question with None rather than
    changing what the answer means. A slot MOVE stamps ``last_location_* = the old slot``,
    so the row that last left this bay is routinely one that now lives in another tray;
    exhaustion is a claim about a roll's contents, and stamping a row the wire says is
    seated somewhere else is precisely the assumption-tier overreach invariant 11 forbids.
    Returning None here is also what keeps ``_bound_after_the_bay_cleared`` honest: with no
    admissible residue there is no alternative victim to name, so tier 1 keeps the live
    binding instead of standing aside in favour of an OLDER residue this function would
    otherwise have had to reach past to find.
    """
    result = await db.execute(spool_binding.last_released_from_slot_stmt(printer_id, ams_id, tray_id).limit(1))
    residue = result.scalar_one_or_none()
    if residue is not None and spool_binding.bound_elsewhere(residue):
        logger.info(
            "[RESPOOL] printer %d AMS%d-T%d: this slot's last occupant, spool %d, is bound elsewhere now — "
            "no last-location victim (invariant 11: a breadcrumb never stamps a roll the wire seats in "
            "another tray, and an older residue is a different roll)",
            printer_id,
            ams_id,
            tray_id,
            residue.id,
        )
        return None
    return residue


async def _mark_tray_spent(db: AsyncSession, printer_id: int, global_tray: int) -> Spool | None:
    """Stamp spent_at on the roll that was in the decoded slot. Idempotent.

    THE one spent writer — all four trigger lanes (unrescued HMS shorts, the firmware's
    per-slot runout words, the HMS-free backup-swap confirmer, and the held-runout
    escalation) funnel through here, so whichever roll this function names is the roll
    the farm calls exhausted. It names it in two tiers, both pointing at the SAME
    physical roll:

    1. **the live binding** on (ams_id, tray_id) — correct whenever the AMS still claims
       the slot at HMS time, and whenever that binding began BEFORE the bay-clear this
       runout is explaining (:func:`_bound_after_the_bay_cleared`);
    2. **the newest row whose last release was FROM this slot**
       (:func:`spool_binding.last_released_from_slot_stmt`), when nothing is bound —
       including the broken shape where an assignment survives its deleted spool row —
       and when tier 1's incumbent was seated after the bay emptied, which is a roll that
       physically cannot have drained it.

    Tier 2 is not a fallback nicety, it is the NORMAL path for a natural runout. The AMS
    clears a drained slot's exist bit ~3 MINUTES BEFORE it declares the filament runout
    (the tail is still traversing the feed path), proven 3× on 2026-08-13: releases at
    03:55:46 / 06:41:47 / 07:02:52 against runout HMS at 03:58:20 / 06:44:44 / 07:05:33.
    That release is CORRECT wire truth — an assignment claims where a roll physically IS
    (doctrine rule 9) and the bay is empty — so exhaustion attribution has to SURVIVE the
    release rather than fight it. It did not: this writer required a live assignment, and
    from the 2026-08-10 release wave (which made bit-clear releases fire reliably for the
    first time) until this change, every spent stamp fleet-wide was a silent no-op. The
    dependency was never written down, which is why no review of that wave could flag it;
    it is written down now.

    **Tier 2 adjudicates THE NEWEST row and never scans past it** — deliberately NOT
    ``WHERE spent_at IS NULL``. A duplicate trigger (the same slot re-raising under a new
    job, where ``_spent_dedup`` no longer absorbs it) must resolve to the already-spent
    victim and no-op; a spent-filtered query would step OVER that victim onto an OLDER
    unspent release residue of the same slot — a healthy shelf roll that once sat there —
    and stamp it permanently. Cross-cutting invariant 11 applied to the query shape: a
    missed stamp self-heals forward, a false one never does.

    **Tier 1 is no longer unconditional, and the case that changed it is the one the
    paragraph above used to dismiss.** "An operator inserting a fresh roll inside the
    3-minute gap wins TIER 1 by construction: inserting it binds the slot" was true, and it
    was the bug: the 2026-08-19 wave made that insertion MINT a fresh row (T7/T8 — a runout
    release is never a glitch), so tier 1 stamped the brand-new roll spent at 0 g used and
    left the roll that actually ran dry un-stamped and still selectable. The eligibility
    test :func:`_bound_after_the_bay_cleared` is the fix, and it is a statement about
    physics rather than a recency threshold on the residue: a roll bound after the bay
    emptied cannot be the roll that emptied it. When it fires, the ladder falls through to
    tier 2 — the row that actually left this slot — and the fresh roll is never touched.

    An ARCHIVED newest victim stands down — retired inventory is never stamped — and so
    does an empty slot history. Both say so in the log, because six silent exits are
    exactly how this failure hid for three days.

    A NEW stamp additionally hands the slot to ``spool_tagless.reattribute_early_runout``,
    the backward direction of the charge re-attribution lane: a roll that ran out far short
    of its assumed label may be a mint the 5-minute de-bounce window produced over a roll
    that never actually left (rule 7's amendment, scenarios T5/T11), and the runout is the
    only moment that is knowable. It is a strictly ADDITIVE hook running after the commit —
    it decides nothing about which row is stamped here, cannot fail into this lane, and
    stands down on anything short of an exact fit.
    """
    ams_id, tray_id = decode_global_tray(global_tray)
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
    spool = assignment.spool if assignment is not None else None
    tier = "assignment"

    # Read once, used by both tiers: the eligibility test below needs the same row tier 2
    # would adjudicate, so resolving it twice could only let them disagree.
    residue = await _newest_released_from_slot(db, printer_id, ams_id, tray_id)

    if spool is not None and _bound_after_the_bay_cleared(
        assignment, residue, printer_id=printer_id, ams_id=ams_id, tray_id=tray_id
    ):
        logger.info(
            "[RESPOOL] tier 1 stands aside on printer %d AMS%d-T%d: spool %d was bound at %s, "
            "AFTER this bay cleared at %s (%.0fs ago) — a roll seated after the bay emptied cannot "
            "be the roll that drained it, so the exhaustion belongs to the released row",
            printer_id,
            ams_id,
            tray_id,
            spool.id,
            assignment.created_at,
            residue.last_location_at,
            (datetime.utcnow() - residue.last_location_at).total_seconds(),
        )
        spool = None

    if spool is None:
        # --- tier 2: the slot released its roll before the firmware admitted why ---
        tier = "last_location"
        spool = residue
        if spool is None:
            logger.info(
                "no live assignment and no last-location victim — spent stamp suppressed (printer %d AMS%d-T%d)",
                printer_id,
                ams_id,
                tray_id,
            )
            return None
        # Adjudicate this row, whatever it says — walking on would land on an older
        # residue of the same slot, which is a different (healthy) roll.
        if spool.spent_at is not None:
            logger.info(
                "Spool %d already spent (printer %d AMS%d-T%d, tier=last_location) — duplicate runout trigger "
                "for the same slot, no re-stamp",
                spool.id,
                printer_id,
                ams_id,
                tray_id,
            )
            return spool  # idempotent success: the caller still books its dedup key
        if spool.archived_at is not None:
            logger.info(
                "Last-location victim spool %d is archived (printer %d AMS%d-T%d) — spent stamp stood down; "
                "retired inventory is never stamped",
                spool.id,
                printer_id,
                ams_id,
                tray_id,
            )
            return None

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
    # The runout closes this roll's TRUE capacity, so record what it delivered rather than
    # complaining that the delivery missed an assumption (operator ruling 18). The stamp is
    # already committed below; this is the durable artifact of the figure, which nothing
    # used to produce — the old WARNING fired on the NORMAL shape and so read as noise.
    assumed_g = float(spool.label_weight or 0)
    shortfall_g = assumed_g - float(spool.weight_used or 0)
    if shortfall_g > _SPENT_DELIVERY_GAP_NOTE_G:
        logger.info(
            "Spool %d delivered %.0f g against an assumed %.0f g label (printer %d AMS%d-T%d) "
            "— the runout closes its true capacity; a part-used roll seated as full is the "
            "ordinary cause and is not an error",
            spool.id,
            float(spool.weight_used or 0),
            assumed_g,
            printer_id,
            ams_id,
            tray_id,
        )
    await db.commit()
    logger.info(
        "Marked spool %d spent (printer %d AMS%d-T%d, hardware runout, tier=%s)",
        spool.id,
        printer_id,
        ams_id,
        tray_id,
        tier,
    )

    # The stamp above is the wire's statement and is now durable. What it additionally lets
    # the farm SETTLE is whether this row was ever a separate roll at all: a runout far
    # short of the assumed label is how a mistaken mint announces itself (rule 7's amended
    # 5-minute window mints on every longer absence — scenarios T5/T11 — and that mint
    # over-promises the start gate), and the fix is the BACKWARD direction of the one charge
    # re-attribution lane. Hooked HERE because this is the single spent writer all four
    # trigger lanes funnel through, so the correction gets exactly one entry and the
    # stamping decision above is untouched by it.
    #
    # Deferred import: ``spool_tagless`` imports THIS module at module scope, so the
    # dependency may only run the other way as a call — the same shape
    # ``detect_spent_contradictions`` uses for the forward direction. Fully self-guarding on
    # its own side (invariant 10); it can neither raise into this lane nor alter the stamp.
    from backend.app.services.spool_tagless import reattribute_early_runout

    await reattribute_early_runout(db, spool, printer_id=printer_id, ams_id=ams_id, tray_id=tray_id)
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


def _ams_hint_from_short_codes(short_codes) -> int | None:
    """The AMS unit the triggering runout short codes name, when they agree on ONE.

    ``07XX_8011`` — the slot-agnostic "insert filament into the SAME AMS slot" runout —
    names no slot, which is why it falls through to inference at all. It DOES name its
    unit: a short code's ``MMMM`` half is exactly ``(attr >> 16) & 0xFFFF``
    (:func:`hms_errors.hms_short_code`), so re-widening it recovers the two bytes
    :func:`hms_errors.ams_unit_from_attr` reads — the module class and the unit id.
    One decoder for "which AMS does this attr name"; this never re-implements the
    layout, it only undoes the string formatting.

    ``None`` when the codes name no AMS unit (``0300_8004`` is the extruder-module
    runout — module ``0x03``, not ``0x07``) or when several DISAGREE. Disagreement is
    genuinely unknown, not a tie to break: two units reporting runouts in one push is
    the chained case, and picking either would be the same guess this hint exists to
    stop. Pure; never raises (it runs on the HMS callback path, invariant 10).
    """
    from backend.app.services.hms_errors import ams_unit_from_attr

    units: set[int] = set()
    for short in short_codes or []:
        try:
            module_half = str(short).split("_", 1)[0]
            unit = ams_unit_from_attr(int(module_half, 16) << 16)
        except (TypeError, ValueError):  # noqa: PERF203 — a malformed code must not break the hint
            continue
        if unit is not None:
            units.add(unit)
    return units.pop() if len(units) == 1 else None


def _topology_is_ambiguous(state, client) -> bool:
    """Can this printer's ``tray_now`` be trusted to name a GLOBAL tray by itself?

    ``False`` (unambiguous) only for the one shape where the wire value can mean
    nothing else: a SINGLE AMS unit on a SINGLE-nozzle printer. There ``tray_now`` is
    the global id, ``global == ams_id * 4 + slot`` collapses to ``slot``, and the
    inference tier below is reading firmware truth.

    ``True`` in both other shapes, for the same underlying reason — the firmware sends
    a bare SLOT number and somebody has to decide which unit owns it:

    * **dual nozzle** — ``bambu_mqtt._handle_ams_data`` runs the H2D disambiguation,
      whose fallbacks are explicitly labelled as possibly wrong ("no AMS on extruder
      N, using slot M"; "multiple AMS …, no snow field, using slot M (may be
      incorrect)"). Those two WARNINGs were observed 15+ times on H2C serials.
    * **several AMS units** — even single-nozzle firmwares report a local slot id
      (#420), and a two-unit printer has two candidate global trays per slot.

    A missing client cannot ASSERT dual-nozzle, so it does not make the topology
    ambiguous on its own: in production a live status push and a live client come from
    the same registry, so a state to reason about implies a client to ask, and the
    unit-count half of the test still stands for every printer. Pure; never raises.
    """
    if len(_iter_ams_units(state)) > 1:
        return True
    try:
        return bool(client is not None and client.is_dual_nozzle)
    except Exception:  # noqa: BLE001 — a client probe must never break the HMS callback chain
        return False


def _owe_attribution_evidence(printer_id: int, state, ams_hint: int | None) -> None:
    """Ask the printer (and the identify lane) to settle an attribution we refused.

    Called from the one place that declines to stamp on an ambiguous topology. It buys
    no certainty now; it makes the NEXT runout resolvable, which is the whole shape of
    the fix — a missed stamp self-heals forward, a false one never does.

    Two acts, both fire-and-forget and both bounded:

    * a fresh full report (``request_evidence_pushall``) — the client owns the
      connected check and the pacing floor, and ``False`` just means "not now";
    * a READ OCCASION on each PRESENT slot of the hinted unit. The runout named that
      unit, so the farm's identity ledger for it is what is in doubt; an occasion is
      permission for ONE commanded read, not a read, and ``identify_needed`` still
      decides whether any is warranted (an untagged, unbound slot is never read —
      invariant 4). Paced BY CAUSE (a runout the farm could not attribute), never by a
      timer over a standing verdict, per doctrine rule 6/7.

    Never raises: this runs inside the HMS status callback (invariant 10).
    """
    try:
        from backend.app.services.printer_manager import printer_manager

        printer_manager.request_evidence_pushall(printer_id, "spent_attribution")
    except Exception:  # noqa: BLE001 — evidence is best-effort; the refusal to stamp already stands
        logger.debug("[RESPOOL] evidence pushall failed for printer %s", printer_id, exc_info=True)
    if ams_hint is None:
        return
    try:
        from backend.app.services import ams_presence

        for unit in _iter_ams_units(state):
            if not isinstance(unit, dict) or int(unit.get("id", -1)) != ams_hint:
                continue
            for tray in unit.get("tray", []) or []:
                if not isinstance(tray, dict):
                    continue
                tray_id = tray.get("id")
                if tray_id is None or tray_presence_from_dict(tray) is not True:
                    continue
                ams_presence.open_read_occasion(printer_id, ams_hint, int(tray_id))
    except Exception:  # noqa: BLE001 — same contract
        logger.debug("[RESPOOL] read-occasion owing failed for printer %s", printer_id, exc_info=True)


def _warn_hint_mismatch(printer_id: int, ams_hint: int | None, resolved_unit: int, source: str) -> None:
    """Log a disagreement between the trigger's unit hint and the slot-attributed answer.

    Telemetry ONLY — the attr answer stands unconditionally. Slot-attributed wire truth
    is the firmware naming the exact slot; the hint is a coarser read of a different
    code's module byte, so when they disagree the hint is what is wrong (or the push
    carries runouts from two units at once, which is the chained case). Surfacing it is
    how a systematic misattribution becomes visible instead of silently outvoting
    nothing. Never raises.
    """
    if ams_hint is None or ams_hint == resolved_unit:
        return
    logger.warning(
        "[RESPOOL] runout unit hint disagrees with the firmware's slot attribution on printer %d: "
        "hint names AMS %d, %s names AMS %d — using the slot attribution (misattribution telemetry)",
        printer_id,
        ams_hint,
        source,
        resolved_unit,
    )


async def _resolve_exhausted_tray(
    db: AsyncSession, printer_id: int, state, *, ams_hint: int | None = None
) -> int | None:
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
       **Permitted only on an unambiguous topology** — see below.

    Step 1 is load-bearing for the CHAINED case. The firmware APPENDS newer faults, so
    the first-hit decode returns the OLDEST slot-attributed entry — after a rescue-then-
    unrescued sequence that is the slot the auto-switch trigger has ALREADY stamped,
    whereupon ``_spent_dedup`` swallows this stamp and the actually-unrescued roll is
    never marked. 006-H2S 2026-07-26 carried exactly that list shape (an older
    auto-switched slot-1 entry ahead of the standing slot-3 demand).

    **``ams_hint`` — the unit the trigger named** (:func:`_ams_hint_from_short_codes`),
    and the gate on step 3 (spools 185 & 205, printer 12 / H2C, 2026-07-31). That
    printer runs THREE AMS units behind a dual nozzle, so ``tray_now`` is a bare slot
    number the client disambiguates with explicitly may-be-wrong fallbacks. Step 3
    trusted it anyway and stamped two LOADED rolls reading ``remain = 100 %`` as spent
    with ``weight_used = 0``; they stayed excluded from selection for NINE DAYS. So:

    * steps 1-2 are UNCHANGED — slot-attributed wire truth outranks the hint, which is
      only a unit. A hint that disagrees with the decoded unit is logged as
      misattribution telemetry and the attr answer stands;
    * step 3 runs only when the topology cannot mislead (:func:`_topology_is_ambiguous`:
      exactly one AMS unit, single nozzle). Otherwise a hinted ``tray_now`` is accepted
      only when it decodes INTO the hinted unit, and anything else stamps NOTHING —
      the farm owes evidence instead (:func:`_owe_attribution_evidence`).

    Not stamping is the safe failure and that asymmetry is the entire design: a MISSED
    stamp self-heals forward (the next runout re-fires, the tagless fresh-roll prompt is
    the backstop, and the operator's physical swap surfaces it), while a FALSE stamp is
    effectively permanent — the grams-reconcile deliberately preserves the latch, there
    is no AUTOMATIC un-spend lane by operator ruling, and the row is hard-excluded from
    selection until somebody notices. The single deliberate un-spend is operator-ANSWERED
    and evidence-gated: dismissing the respool prompt (``POST
    /inventory/spools/{id}/respool-dismiss``) NULLs ``spent_at`` when the live AMS remain
    contradicts the stamp — a human looking at the tray, never a lane that clears stamps
    on its own. Never widen step 3 to "guess something rather than nothing".

    The ``isinstance`` guard keeps the decode fail-closed on an absent / non-list
    ``hms_errors`` (MagicMock states in tests, a degenerate push in production), so
    inference stays the fallback rather than raising into a callback."""
    hms_list = getattr(state, "hms_errors", None)
    if isinstance(hms_list, list):
        from backend.app.services.hms_errors import current_runout_demand

        demanded = current_runout_demand(hms_list)
        if demanded is not None:
            _warn_hint_mismatch(printer_id, ams_hint, demanded[0], "demand")
            return demanded[0] * 4 + demanded[1]
    decoded = _runout_slot_global_tray(state)
    if decoded is not None:
        _warn_hint_mismatch(printer_id, ams_hint, decoded // 4, "attr decode")
        return decoded

    # --- step 3: inference, and the topology gate on it ---------------------
    from backend.app.services.printer_manager import printer_manager

    ambiguous = _topology_is_ambiguous(state, printer_manager.get_client(printer_id))
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

    # The inference itself is UNCHANGED — it is computed once here so the topology
    # gate below applies to every one of its outcomes rather than to some of them.
    candidate: int | None = None
    mapped = False
    if item and item.ams_mapping:
        try:
            mapping = json.loads(item.ams_mapping)
            feeders = [int(v) for v in mapping if isinstance(v, (int, float)) and int(v) >= 0]
        except (ValueError, TypeError):
            feeders = []
        if len(feeders) == 1:
            # Single-feeder farm job: the live feeding tray is authoritative; the
            # mapping is only a fallback for an unloaded/unknown tray_now.
            candidate, mapped = (tray_now if live_ok else feeders[0]), True
        elif feeders:
            # Multi-filament job: the mapping alone can't say WHICH feeder ran
            # out. Trust the live tray_now only when it is one of the job's
            # feeders; otherwise mark nothing (fail-safe — a wrong spent stamp
            # would auto-reset a half-full spool to fresh on its next arrival).
            candidate, mapped = (tray_now if tray_now is not None and tray_now in feeders else None), True
    if not mapped and live_ok:
        candidate = tray_now

    if candidate is None:
        # The last silent exit in the stamp path. Every tier declined: the firmware named
        # no slot (no demand, nothing slot-attributed decoded), and inference had nothing
        # admissible either — no single-feeder job, or a multi-feeder job whose tray_now is
        # not one of its own feeders (the deliberate multi-filament fail-safe). Not
        # stamping is correct here; being unable to tell that from a dead lane is not.
        logger.info(
            "[RESPOOL] runout attribution on printer %d: the firmware named no slot and inference "
            "produced no admissible candidate — not stamping",
            printer_id,
        )
        return None
    if not ambiguous:
        return candidate
    # AMBIGUOUS topology: `tray_now` is a bare slot the client had to guess a unit
    # for, so the inference is admissible only where the firmware's own unit hint
    # corroborates it. `candidate // 4` is the decoded unit for a regular AMS global
    # id; AMS-HT (>=128) and the external sentinels carry no unit to corroborate and
    # so can never clear this gate.
    if ams_hint is not None and 0 <= candidate <= 127 and candidate // 4 == ams_hint:
        logger.info(
            "[RESPOOL] runout attribution on printer %d: tray_now-derived tray %d decodes into the "
            "firmware-hinted AMS %d — accepting on an ambiguous topology",
            printer_id,
            candidate,
            ams_hint,
        )
        return candidate
    logger.warning(
        "[RESPOOL] runout attribution ambiguous (multi-AMS/dual-nozzle) — not stamping; owe evidence "
        "(printer %d, inferred tray %s, firmware AMS hint %s). tray_now is a bare slot on this "
        "topology and the firmware named no slot, so a stamp here would be a guess — and a false "
        "spent stamp is permanent (spools 185/205, 2026-07-31), while a missed one self-heals.",
        printer_id,
        candidate,
        "none" if ams_hint is None else ams_hint,
    )
    _owe_attribution_evidence(printer_id, state, ams_hint)
    return None


async def apply_runout_edges(printer_id: int, edges, state, session_factory=None) -> None:
    """Tier 1 entry point: route ONE wire-HMS appearance edge into both spent stampers.

    ``edges`` is an :class:`hms_edges.HmsEdgeReport` — the codes that APPEARED on the
    frame ``main`` just consumed. That tracker, not the notification dedup, is what
    "NEW" means for spent evidence: its 600 s re-notify window is a level-triggered
    ALERT policy, and a runout re-appearing inside it under a different job is a second
    physical exhaustion that must stamp a second time. Restart-replay suppression is the
    tracker's own first-frame seed (a code live at first sight never edges), which is why
    neither stamper carries a seed check any more.

    Both lanes are derived from ``edges.appeared`` — the live ``HMSError`` entries, which
    are UNHASHABLE (mutable dataclass), so they are read into codes rather than sets:

    * Lane A (:func:`mark_spent_on_runout`) — appeared short codes ∩
      :data:`RUNOUT_HMS_CODES`, the UNRESCUED vocabulary;
    * Lane B (:func:`mark_spent_on_slot_runout`) — per-entry ``(full_code, attr,
      code_word)`` for the firmware's own PER-EVENT, SLOT-ATTRIBUTED runout statements
      (the pull-back notice and the auto-switch report), each of which decodes a real
      slot. The bare demand is deliberately excluded upstream in
      :data:`hms_errors._RUNOUT_SLOT_SPENT_CODE32`.

    Accepted residual: a runout that fired entirely DURING server downtime — living and
    dying inside the gap, so it is neither live at the first consumed frame nor ever
    seen to appear — never stamps spent. The tray's true state across the gap is
    unknowable and a replayed stamp would mark whatever roll is in the slot NOW (the
    2026-07-17 18:56 fresh-spool misattribution), so we fail safe. That case is bounded
    by the pause-stall watchdog (the print sits PAUSEd and escalates), the tagless
    fresh-roll prompt, and the Tier-3 respool prompt when the reused tag next arrives.

    Fire-and-forget from ``main``: it owns its session (``session_factory`` is the test
    seam, ``None`` meaning :data:`core.database.async_session`) and the whole body is
    guarded, because an exception here would land in an orphaned task and must never
    reach the MQTT status chain (fork invariant 10).
    """
    try:
        from backend.app.services.hms_errors import (
            _RUNOUT_SLOT_SPENT_CODE32,
            _code_word,
            runout_slot_from_hms,
        )

        appeared = edges.appeared
        lane_a = {hms_short_code(e.attr, e.code) for e in appeared} & RUNOUT_HMS_CODES
        lane_b = [
            (e.full_code, int(e.attr or 0), _code_word(e.code))
            for e in appeared
            if _code_word(e.code) in _RUNOUT_SLOT_SPENT_CODE32
            and runout_slot_from_hms(int(e.attr or 0), _code_word(e.code)) is not None
        ]
        if not lane_a and not lane_b:
            return
        if session_factory is None:
            from backend.app.core.database import async_session

            session_factory = async_session
        async with session_factory() as db:
            if lane_a:
                await mark_spent_on_runout(db, printer_id, lane_a, state)
            if lane_b:
                await mark_spent_on_slot_runout(db, printer_id, lane_b, state)
    except Exception as e:  # noqa: BLE001 — a fire-and-forget task must never raise
        logger.warning("Runout-edge apply failed for printer %s: %s", printer_id, e)


async def mark_spent_on_runout(db: AsyncSession, printer_id: int, new_short_codes, state) -> Spool | None:
    """Tier 1: a NEW runout HMS code stamps spent_at on the exhausted tray's spool.

    "NEW" means the code APPEARED on the wire-HMS edge tracker (``services.hms_edges``,
    via :func:`apply_runout_edges`) — not that the notification dedup called it new.
    Restart-replay suppression is that tracker's first-frame seed: a code already live
    when the process first sees the printer never edges, so a swapped-in fresh spool
    cannot be mis-stamped by a pre-restart runout.

    Resolves the exhausted tray via :func:`_resolve_exhausted_tray` — firmware slot
    attribution first, the dispatched farm ``ams_mapping`` / live ``tray_now`` only as
    inference. Idempotent: re-observing the code is a no-op once spent_at is set. No-op
    in Spoolman mode.

    The TRIGGERING codes carry one more fact than the vocabulary check consumes: their
    module half names the AMS unit (``07XX_8011`` → unit XX; the extruder-module
    ``0300_8004`` names none). That unit is passed down as ``ams_hint`` and is what
    gates the inference tier on a printer whose ``tray_now`` is a bare slot number —
    the 185/205 misattribution class. See :func:`_resolve_exhausted_tray`.
    """
    if await _spoolman_enabled(db):
        return None
    triggering = set(new_short_codes) & RUNOUT_HMS_CODES
    if not triggering:
        return None
    global_tray = await _resolve_exhausted_tray(db, printer_id, state, ams_hint=_ams_hint_from_short_codes(triggering))
    if global_tray is None:
        return None
    # Incident dedup: one spent stamp per (printer, job, tray). A re-raised runout
    # on the same job/tray must not stamp the operator's freshly-inserted spool.
    subtask_id = getattr(state, "subtask_id", None)
    key = (printer_id, subtask_id, global_tray)
    if key in _spent_dedup:
        # Bounded and per-event: the dedup key carries the job, so this fires once per
        # re-raise of the SAME runout on the same slot under the same job. It is a log
        # line because the alternative is what hid the 2026-08-13 failure for three days —
        # a suppressed stamp and a dead lane are indistinguishable from silence.
        logger.info(
            "[RESPOOL] spent stamp already booked for printer %d job %s tray %d — dedup",
            printer_id,
            subtask_id,
            global_tray,
        )
        return None
    spool = await _mark_tray_spent(db, printer_id, global_tray)
    if spool is not None:
        _spent_dedup.add(key)
    return spool


async def mark_spent_on_slot_runout(db: AsyncSession, printer_id: int, events, state) -> list[Spool]:
    """Tier 1: a NEW firmware PER-EVENT runout word stamps spent_at on the slot IT named.

    The sibling of :func:`mark_spent_on_runout` for the cases that trigger structurally
    cannot see. ``RUNOUT_HMS_CODES`` is the UNRESCUED, slot-AGNOSTIC vocabulary
    (0300_8004 + 07xx_8011 — "insert filament", print held), and it is both too narrow
    and too coarse on its own:

    * a runout the AMS backup RESCUED raises none of it — it raises only the
      slot-attributed ``0700_2X00`` family and keeps printing (fleet evidence
      2026-07-30: four confirmed auto-refills across three printers, zero spent stamps;
      the un-stamped rows then surfaced as spurious "Fresh roll?" prompts on the
      eventual physical swap, where a SPENT row takes the silent spent→mint path);
    * the 8011 a TERMINAL runout does raise names no slot and LATCHES, so it depends
      entirely on an appearance edge that a deploy re-seeds away.

    Both are covered by the two PER-EVENT words this trigger consumes
    (:data:`hms_errors._RUNOUT_SLOT_SPENT_CODE32`): the pull-back notice 0x00030001 and
    the auto-switch report 0x00030002. They fire in a fixed order and cost nothing to
    have both — on a TERMINAL runout only 0x30001 ever arrives (003-H2S 2026-08-13:
    ~12 s before the demand, and the auto-switch can never come because there is no
    backup slot left to switch to); on a RESCUED runout 0x30001 is followed by
    0x30002 naming the SAME slot, and ``_spent_dedup`` absorbs the second.

    ``events`` are ``(full_code, attr, code_word)`` tuples the caller has already
    established APPEARED on this frame's wire-HMS edge (``services.hms_edges``, via
    :func:`apply_runout_edges`) — which is also where restart-replay suppression lives
    now: a code live at first sight never edges, so it can never stamp the roll an
    operator swapped in during the downtime. Attribution is ATTR-PRIMARY and PER EVENT — the
    firmware names the exhausted slot in the attr, and one print can chain several
    (005-H2S 2026-07-30 ran three rolls dry inside a single job, its completion gram
    split proving four trays fed). Resolving once per push, or through tray_now, would
    stamp only one of them; the per-event loop stamps each, and each has its own
    ``_spent_dedup`` key so a re-raise of the same slot in the same job still can't
    double-stamp the roll the operator has since inserted.

    Only :data:`hms_errors._RUNOUT_SLOT_SPENT_CODE32` may reach here — see that constant
    for why the bare demand (0x00020001) is NOT spent evidence: 006-H2S proved firmware
    can latch a bogus demand for a slot that never ran dry, and a demand-driven stamp
    would have archived a healthy roll.

    **This trigger needs no topology gate.** Attribution is `runout_slot_from_hms(attr,
    code_word)` and NOTHING else: an entry whose attr does not decode to a real slot is
    skipped outright, so there is no path from here into ``_resolve_exhausted_tray``'s
    ``tray_now`` inference and none into the 185/205 misattribution class. Keep it that
    way — the moment this loop gains a fallback it inherits the same gate.

    Returns the spools stamped by THIS call (empty when nothing qualified). No-op in
    Spoolman mode; delegates the stamp itself to the shared :func:`_mark_tray_spent` so
    the idempotency and fat-remainder WARNING are identical.
    """
    if await _spoolman_enabled(db):
        return []
    from backend.app.services.hms_errors import _RUNOUT_SLOT_SPENT_CODE32, runout_slot_from_hms

    subtask_id = getattr(state, "subtask_id", None)
    stamped: list[Spool] = []
    # ``_full_code`` is the edge tracker's lossless identity for the entry; the stamp
    # itself attributes purely from the attr, so it is unpacked for shape only.
    for _full_code, attr, code_word in events or []:
        # Re-assert the trigger vocabulary here, exactly as :func:`mark_spent_on_runout`
        # re-intersects RUNOUT_HMS_CODES despite its caller having filtered too. The
        # wider slot-attributed family decodes a slot just fine, so a caller that hands
        # over a bare DEMAND would silently archive a healthy roll — the 006 false-stamp
        # class. The narrow set is this function's contract, so it fails closed on it.
        if code_word not in _RUNOUT_SLOT_SPENT_CODE32:
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
            # The NORMAL rescued sequence lands here: 0x30001 stamps, 0x30002 names the
            # same slot moments later and is absorbed. Saying so is what makes "absorbed"
            # distinguishable from "never fired" in a prod log.
            logger.info(
                "[RESPOOL] spent stamp already booked for printer %d job %s tray %d — dedup",
                printer_id,
                subtask_id,
                global_tray,
            )
            continue
        spool = await _mark_tray_spent(db, printer_id, global_tray)
        if spool is not None:
            _spent_dedup.add(key)
            stamped.append(spool)
    return stamped


async def mark_spent_on_runout_hold(printer_id: int, state, *, subtask_id, session_factory=None) -> None:
    """Tier 1, the DURABLE lane: a HELD runout's ESCALATION stamps the exhausted slot.

    Called from ``spool_recovery._escalate`` for ``runout_needs_refill`` only. The three
    edge-driven lanes above all hang off ``hms_edges`` appearance edges, and those are
    EPHEMERAL by construction: the first frame a process consumes SEEDS instead of
    edging, so every restart re-seeds whatever runout is standing and no later frame can
    ever edge it again. A terminal runout holds for hours (003-H2S 2026-08-13 held
    12.5 h) — long enough for a deploy to land inside it — and its only non-latching,
    slot-attributed word (0x00030001) fired seconds after the runout began, i.e. before
    the deploy. This lane closes that gap: an ESCALATION is a durable, incident-anchored
    event derived from the LIVE HMS list, so it fires on the boot path too (standing
    PAUSE codes at startup → incident → ``_run_recovery`` → ``_escalate``).

    It is deliberately a SECOND TRIGGER, not a second writer: everything below funnels
    into :func:`_mark_tray_spent` with the same dedup key shape as its siblings, so a
    runout seen by both an edge and an escalation stamps exactly once.

    **The slot is RE-RESOLVED here, never taken from the caller** (D3). The incident row
    carries a ``slot_global_tray`` whose provenance may be un-gated ``tray_now``
    inference — the spools 185/205 class, where a bare slot number on a multi-AMS,
    dual-nozzle printer was guessed into a unit. :func:`_resolve_exhausted_tray` is the
    one resolver that applies the topology gate, so this lane inherits its fail-closed
    behaviour instead of laundering a guess into a permanent stamp.

    **The 006 stand-down** (D2): stamp ONLY when the resolved slot reads wire-asserted
    EMPTY. A genuine terminal runout emptied its bay MINUTES before the firmware admitted
    why (the release-before-runout ordering, three timed pairs on 2026-08-13), so by
    escalation time the merged copy asserts the cleared shape. A BOGUS latched demand —
    006-H2S 2026-07-26, where a load command issued during a hold resurfaced 12 h later
    as a demand for a slot that never ran dry — names a slot that is still OCCUPIED.
    Presence is read through the one canonical predicate
    (:func:`tray_fields.tray_presence_from_dict`), and only ``False`` proceeds: ``True``
    is the bogus-latch shape and ``None`` is no evidence at all. Standing down costs
    nothing here — the edge lanes own the edge-time stamp, and a missed stamp self-heals
    forward while a false one is permanent (invariant 11).

    External-holder runouts never reach this function: the caller gates on the escalation
    REASON, and ``external_spool_runout`` is excluded because the left/right vt-tray
    attribution convention is unconfirmed (a wrong-side stamp on a dual-holder model
    would be permanent).

    Documented residual: an incident that has ALREADY escalated and is still open across
    a restart re-fires nothing — but its escalation ran pre-restart under this same code,
    so the stamp already landed. Nothing re-escalates a hold that is already held.

    Owns its session (``session_factory`` is the test seam) and never raises: an
    escalation must not fail because bookkeeping did (invariant 10). No-op in Spoolman
    mode — Spoolman owns the ledger there.
    """
    try:
        if session_factory is None:
            from backend.app.core.database import async_session

            session_factory = async_session
        async with session_factory() as db:
            if await _spoolman_enabled(db):
                return
            global_tray = await _resolve_exhausted_tray(db, printer_id, state)
            if global_tray is None:
                logger.info(
                    "[RESPOOL] runout-hold stamp on printer %d: no slot resolved from the live evidence — "
                    "not stamping (the resolver's own refusal already said why)",
                    printer_id,
                )
                return
            ams_id, tray_id = decode_global_tray(global_tray)
            if ams_id is None or tray_id is None:
                logger.info(
                    "[RESPOOL] runout-hold stamp on printer %d: resolved tray %s decodes to no AMS slot — not stamping",
                    printer_id,
                    global_tray,
                )
                return
            presence = tray_presence_from_dict(_resolve_live_tray(state, ams_id, tray_id))
            if presence is True:
                logger.info(
                    "[RESPOOL] runout-hold stamp on printer %d AMS%d-T%d: the demanded slot reads OCCUPIED — "
                    "bogus-latch shape (006-H2S 2026-07-26), not stamping. A roll that truly ran dry left its "
                    "bay minutes before the firmware said so",
                    printer_id,
                    ams_id,
                    tray_id,
                )
                return
            if presence is None:
                logger.info(
                    "[RESPOOL] runout-hold stamp on printer %d AMS%d-T%d: slot presence is UNKNOWN — not "
                    "stamping; this durable lane needs a wire-asserted empty bay, and the edge lanes own the "
                    "edge-time stamp",
                    printer_id,
                    ams_id,
                    tray_id,
                )
                return
            # Same key shape as both edge stampers — one stamp per (printer, job, tray),
            # whichever trigger gets there first.
            key = (printer_id, subtask_id, global_tray)
            if key in _spent_dedup:
                logger.info(
                    "[RESPOOL] runout-hold stamp on printer %d AMS%d-T%d: already stamped for job %s — "
                    "the edge lane got there first",
                    printer_id,
                    ams_id,
                    tray_id,
                    subtask_id,
                )
                return
            spool = await _mark_tray_spent(db, printer_id, global_tray)
            if spool is not None:
                _spent_dedup.add(key)
    except Exception as e:  # noqa: BLE001 — an escalation must never fail because a stamp did
        logger.warning("Runout-hold spent stamp failed for printer %s: %s", printer_id, e)


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


# Last-seen firmware auto-refill BACKUP GROUPS per printer, as membership lists.
# Refreshed by the per-push sampler whenever a push carries the field and left standing
# when it does not: firmware clears and refills `filam_bak` on every report it appears
# in, but `bambu_mqtt` preserves only ams / vt_tray / ams_extruder_map / mapping across
# raw_data replacement, so an incremental push that omits it would otherwise read as
# "no groups". Machine-scoped, NOT AMS-scoped — the field is top-level or per-EXTRUDER
# on the wire, never per AMS unit (see `tray_fields.parse_filam_bak`). Process-lifetime
# like the other edge dicts; a restart simply re-learns it from the next push carrying
# one, and until then the corroboration below fails safe.
_filam_bak_groups: dict[int, list[list[int]]] = {}


def _note_filam_bak(printer_id: int, state) -> None:
    """Refresh the cached backup groups from this push, if it carries any.

    Sync, pure in-memory, no DB and no awaits — it rides :func:`sample_status_push` on
    the ~1 Hz status callback. Reads BOTH wire shapes and keeps each as its OWN group,
    because "same group" is the question the corroboration asks: the flat machine-level
    ``print.filam_bak`` is one group, and each entry of
    ``print.device.extruder.info[]`` is a separate one (a dual-nozzle machine's two
    nozzles do not back each other up). A push carrying neither leaves the cache alone.
    """
    raw = getattr(state, "raw_data", None)
    if not isinstance(raw, dict):
        return
    groups: list[list[int]] = []
    flat = parse_filam_bak(raw)
    if flat is not None:
        groups.append(flat)
    device = raw.get("device")
    extruder = device.get("extruder") if isinstance(device, dict) else None
    info = extruder.get("info") if isinstance(extruder, dict) else None
    if isinstance(info, list):
        for entry in info:
            per_extruder = parse_filam_bak(entry)
            if per_extruder is not None:
                groups.append(per_extruder)
    if groups:
        _filam_bak_groups[printer_id] = groups


def _backup_swap_corroborated(printer_id: int, departed: int, arrived: int) -> bool | None:
    """Do the departed feeder and its replacement sit in ONE firmware backup group?

    ``True`` / ``False`` when grouping evidence exists, ``None`` when it does not (no
    push has ever carried ``filam_bak`` for this printer). The caller treats ``None``
    and ``False`` alike — no stamp — but they are different facts and the log says which.

    Both trays must appear in the SAME list. That is the literal meaning of a backup
    group: the firmware switched from one enrolled tray to another enrolled tray, which
    is the auto-refill it performs precisely because the first ran dry. A feeder change
    ACROSS groups is something else (a tool change, a dispatch remap) and must never
    spend a spool.

    Encoding safety: ``departed``/``arrived`` are global tray ids, and `filam_bak`'s own
    encoding is unconfirmed. If it turns out to be per-unit slot ids, every tray outside
    AMS 0 has a global id ≥ 4 that no slot id can equal, so this returns ``False`` and we
    decline to stamp. The unconfirmed reading can therefore only cost a stamp, never
    fabricate one — which is the direction this whole workstream fails in.
    """
    groups = _filam_bak_groups.get(printer_id)
    if not groups:
        return None
    return any(departed in group and arrived in group for group in groups)


def _swap_stamp_permitted(printer_id: int, state, departed: int, arrived: int) -> bool:
    """May a confirmed feeder departure stamp the departed spool spent?

    On an UNAMBIGUOUS topology (one AMS unit, single nozzle) — yes, unchanged: the
    stable-feeder machinery above is the whole gate and ``tray_now`` means exactly what
    it says.

    On an ambiguous one (several AMS units, or a dual nozzle) ``tray_now`` is a bare slot
    the client had to guess a unit for, so a "departure" can be an artefact of that guess
    rather than a roll running dry — the same inference that stamped spools 185 and 205
    spent while they sat loaded and full. There the firmware's OWN backup grouping has to
    corroborate it: if it did not have both trays enrolled together, it did not perform an
    auto-refill between them, and there is no run-dry to record.

    Declining costs one stamp and keeps two backstops (the fat-remainder WARNING in
    :func:`_mark_tray_spent` and the tagless fresh-roll prompt at the operator's physical
    swap). Stamping wrongly costs a healthy roll, permanently. Sync and never raises —
    it runs inside the status callback (invariant 10).
    """
    try:
        from backend.app.services.printer_manager import printer_manager

        if not _topology_is_ambiguous(state, printer_manager.get_client(printer_id)):
            return True
    except Exception:  # noqa: BLE001 — an unreadable topology is an ambiguous one
        logger.debug("[RESPOOL] topology probe failed for printer %s", printer_id, exc_info=True)
    corroborated = _backup_swap_corroborated(printer_id, departed, arrived)
    if corroborated:
        return True
    logger.warning(
        "[RESPOOL] backup-swap spent stamp declined on printer %d (tray %d -> %d): ambiguous topology "
        "(multi-AMS/dual-nozzle) and the firmware's backup grouping %s. tray_now is a bare slot here, so "
        "the departure alone is not evidence a roll ran dry — the fat-remainder warning and the "
        "fresh-roll prompt remain the backstops.",
        printer_id,
        departed,
        arrived,
        "does not pair these trays" if corroborated is False else "was never reported",
    )
    return False


def _resolve_pending_swap(printer_id: int, current: int, running: bool) -> tuple[int, int] | None:
    """Resolve an open pending backup swap against the current push; returns
    ``(departed, arrived)`` global trays when it confirms, else ``None``.

    The ARRIVED tray is carried out alongside the departed one because the corroboration
    gate (:func:`_swap_stamp_permitted`) asks about the PAIR — "did the firmware switch
    between two trays it had grouped?" — and this is the only place both halves of the
    edge are known.

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
        return (prev, cur)
    if current != cur:
        _pending_swaps.pop(printer_id, None)  # moved off `cur` before confirming → transient
        return None
    return None  # still on `cur`, within the window → keep waiting


def sample_status_push(printer_id: int, state) -> list[int]:
    """Tier 1: seamless AMS backup-swap detector (runout with no HMS), corroborated —
    the SAMPLING half. Returns the departed global trays whose pending swap CONFIRMED
    on this push (hand them to :func:`confirm_backup_swaps`).

    Called on EVERY status push (``main.on_printer_status_change``, ~1 Hz per printer)
    beside the wire-HMS edge tracker, because that is the only cadence at which the
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

    # Learn the firmware's backup grouping from this push while we are here — it is the
    # corroboration the ambiguous-topology gate below needs, and this is the only lane
    # that sees every push. Cheap and unconditional: a push without the field is a no-op.
    _note_filam_bak(printer_id, state)

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
    #
    # A confirmed edge still has to clear the topology gate: on a printer where
    # ``tray_now`` is a bare slot number the edge may be an artefact of the client's
    # unit guess rather than a roll running dry, and only the firmware's own backup
    # grouping can tell the two apart (:func:`_swap_stamp_permitted`).
    resolved = _resolve_pending_swap(printer_id, current, running)
    confirmed: list[int] = []
    if resolved is not None and _swap_stamp_permitted(printer_id, state, resolved[0], resolved[1]):
        confirmed.append(resolved[0])

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

    Every DECISION about whether a departure deserves a stamp — the commanded-load
    suppression, the stable-feeder requirement, the job boundary, and since 2026-08-09
    the ambiguous-topology corroboration (:func:`_swap_stamp_permitted`) — belongs to
    the sampler, which is the only half that sees both ends of the edge and the live
    push. This half stamps what it is handed.
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


# --- the spent-contradiction detector ---------------------------------------
#
# A spent stamp is a one-way door. `_mark_tray_spent` deliberately leaves the gram
# ledger intact so a false stamp is losslessly reversible IN PRINCIPLE, but by operator
# ruling there is no AUTOMATIC un-spend LANE: the fix for a false stamp is to stop
# producing them (D1 above), never to build machinery that clears them on its own. The
# one deliberate exception is operator-ANSWERED and evidence-gated — dismissing the
# respool prompt (`POST /inventory/spools/{id}/respool-dismiss`) NULLs `spent_at` when
# the live AMS remain contradicts the stamp, which is a human looking at the tray, not
# a detector acting alone. Meanwhile a spent row is hard-excluded from selection
# (`spool_selection.SlotInventory.spent`) and dropped by `filament_deficit`, so the roll
# silently leaves service.
#
# Nothing compared the stamp against the wire. Spools 185 and 205 (printer 12, an H2C
# with three AMS units behind a dual nozzle) were stamped spent on 2026-07-31 with
# `weight_used = 0` while their trays kept reporting LOADED at `remain = 100 %`. Nine
# days. This detector is what makes that class LOUD within minutes: it reads both facts
# every reconcile pass and reports the disagreement.
#
# It NEVER writes a spool row — not the stamp, not the grams, not the archive flag. Its
# entire output is a WARNING plus one notification. That restraint is the design: an
# automatic correction here would be a second guess layered on the first, and the roll
# the ledger describes may genuinely be gone (an operator can seat a DIFFERENT roll of
# the same colour in the same slot). A human settles it.

# Durable notify scope. Its own key space, deliberately not the printer-HMS one: an HMS
# key is `{printer_id}:{full_code}` and records "we alerted on this fault", while these
# are keyed by SPOOL and record "we reported this row's stamp as contradicted". Sharing
# a scope would let an HMS prune or an HMS window silently govern this lane.
_SPENT_CONTRADICTION_SCOPE = "spent_contradiction"

# Wire remain% at or above which a still-seated spent roll is a CONTRADICTION rather
# than ordinary end-of-roll drift. 30 points of label is the margin the respool
# remain-jump already treats as far above AMS quantization noise
# (:data:`_RESPOOL_REMAIN_JUMP_PCT`) — a roll that truly ran dry does not read a third
# full, whatever the ledger says.
_SPENT_CONTRADICTION_MIN_REMAIN_PCT = 30

# The contradiction is a STANDING state: it re-derives identically on every pass until
# a human resolves it, so the re-notify window is long. Seven days keeps a genuinely
# ignored contradiction from going silent forever while never becoming a nag.
_SPENT_CONTRADICTION_RENOTIFY_S = 7 * 24 * 3600.0

# Floor between detector passes. The reconcile tick that hosts it runs every ~20 s and
# this lane walks every spent binding in the fleet plus a durable ledger read per hit —
# far too expensive for that cadence, and pointless at it (a contradiction that has
# stood for nine days does not need sub-minute detection). Mirrors
# ``spool_tagless._last_reconcile_at``: one module-level stamp, no other memory, so the
# pass stays restart-safe and re-derived.
_SPENT_CONTRADICTION_MIN_INTERVAL_S = 900.0
_last_contradiction_scan_at: float | None = None


def _wire_remain_pct(tray: dict) -> int | None:
    """The tray's AMS ``remain`` as a usable percentage, or ``None``.

    ONE parse discipline for the whole module: an integer 1..100 is a reading, and
    everything else is "no reading" — ``0`` and ``-1`` are firmware's own no-value
    sentinels (a tagless tray always reports ``-1``, doctrine rule 8), an absent key
    says nothing, and garbage is not a number. Consumed by the respool prompt payload,
    the remain-jump margin and the spent-contradiction detector, which must never
    disagree about whether the wire said anything at all.
    """
    try:
        remain = int(tray.get("remain"))
    except (TypeError, ValueError):
        return None
    return remain if 1 <= remain <= 100 else None


def _binding_identity_holds(spool: Spool, tray: dict) -> bool:
    """Is the roll in this tray the SAME one the binding claims — as far as it can tell?

    TAGGED binding (the row carries a tag or a uuid): identity is RFID, read under the
    sibling-tag law. ``tray_uuid`` IS the spool identity and ``tag_uid`` is a read of
    one of the roll's two chips, so uuid agreement settles it even across an apparent
    tag change (doctrine rule 10, false-alarm shape 20), and a tag match against EITHER
    recorded chip (:func:`tag_matches_row`) is equally conclusive when the push carried
    no uuid. Matching the near chip alone made the spent-contradiction detector skip a
    genuine contradiction whenever the tray happened to be showing its far side — the
    detector's whole job is to be loud, so a silent miss on a coin-flip of roll
    orientation is the worst possible failure for it. Disagreement on both = a different
    roll, which is not a contradiction at all — it is an ordinary swap the binding has
    not caught up with.

    UNTAGGED binding: no RFID to compare, so the codebase's existing fingerprint idiom
    (``spool_tagless.fingerprint_matches`` — canonical material plus colour within
    tolerance) is the only available test. It is deliberately weak evidence: a different
    roll of the same filament passes it. That is tolerable here and ONLY here, because
    this detector's whole output is a warning a human reads — the same-core ambiguity
    that bars a tagless no-tag read from CONCLUDING anything in ``slot_state`` would bar
    this too if it mutated a row. It does not.
    """
    spool_uuid = normalized_tray_uuid(spool.tray_uuid)
    spool_tag = normalized_tag_uid(spool.tag_uid)
    spool_sibling = normalized_tag_uid(spool.sibling_tag_uid)
    if spool_uuid is not None or spool_tag is not None or spool_sibling is not None:
        tray_uuid = normalized_tray_uuid(tray.get("tray_uuid"))
        tray_tag = normalized_tag_uid(tray.get("tag_uid"))
        return (spool_uuid is not None and spool_uuid == tray_uuid) or tag_matches_row(
            tray_tag, spool_tag, spool_sibling
        )
    from backend.app.services.spool_tagless import fingerprint_matches

    return fingerprint_matches(spool, tray)


async def detect_spent_contradictions(db: AsyncSession, manager=None, *, now: float | None = None) -> int:
    """Report every spool whose SPENT stamp the wire currently contradicts.

    A contradiction is all four of: the row is spent and not archived; it still holds a
    live :class:`SpoolAssignment`; that slot's live tray reads PRESENT with the SAME
    bound identity (:func:`_binding_identity_holds`); and the wire reports it at least
    :data:`_SPENT_CONTRADICTION_MIN_REMAIN_PCT` full. Returns how many were found.

    **It never writes a spool row.** See the section comment above for why an automatic
    correction is the wrong answer. The only durable write on this path is the notify
    dedup ledger entry, so that a standing contradiction alerts once per
    :data:`_SPENT_CONTRADICTION_RENOTIFY_S` instead of on every pass — the state
    re-derives identically forever, which is exactly what a durable window is for (the
    in-memory :func:`notify_dedup.allow` gate would re-blast the fleet at every deploy,
    the failure that made the HMS lane durable in the first place).

    Throttled to :data:`_SPENT_CONTRADICTION_MIN_INTERVAL_S` because its host tick runs
    ~45× faster than this walk is worth. ``manager``/``now`` are injectable for tests.

    **This is also the throttled entry for the fleet's other ledger-integrity sweep**,
    ``spool_tagless.reconcile_ledger_overcharges`` (2026-08-12, 009-H2S spool 290): the two
    ask the same shape of question — does a durable claim on a spool row still survive
    contact with the evidence — at the same cost and the same worthwhile cadence, so they
    share ONE floor and ONE Spoolman gate rather than racing two. The sibling sweep lives
    in ``spool_tagless`` because it needs that module's mint + bind mechanics, which cannot
    be imported here (``spool_tagless`` imports THIS module at module scope); the deferred
    call below is the one direction with no cycle. Its return value stays out of this
    function's count, which remains "contradictions found" alone.

    FULLY self-guarding (invariant 10), at two levels: per row, so one unreadable
    printer cannot abort the sweep, and around the whole sweep, so nothing here can kill
    the scheduler-tick lane it hangs off. That is what lets the call site be a bare line
    with no try/except of its own — this is an entry hook, and an entry hook owns its
    own guard.
    """
    global _last_contradiction_scan_at

    now = _monotonic() if now is None else now
    if _last_contradiction_scan_at is not None and (now - _last_contradiction_scan_at) < (
        _SPENT_CONTRADICTION_MIN_INTERVAL_S
    ):
        return 0
    _last_contradiction_scan_at = now

    if await _spoolman_enabled(db):
        return 0  # Spoolman owns the spool lifecycle; the farm's spent stamp is not the truth there

    if manager is None:
        from backend.app.services.printer_manager import printer_manager

        manager = printer_manager

    try:
        found = await _scan_spent_contradictions(db, manager)
    except Exception:  # noqa: BLE001 — an entry hook owns its guard; the tick must survive
        logger.exception("Spent-contradiction sweep failed (non-fatal)")
        found = 0

    # The sibling ledger-integrity sweep, under this entry's throttle and Spoolman gate
    # (see the docstring). Deferred import: ``spool_tagless`` imports this module at module
    # scope. Called bare on purpose — it is an entry hook that owns its own guard, so a
    # try/except here would be the second guard on one contract.
    from backend.app.services.spool_tagless import reconcile_ledger_overcharges

    await reconcile_ledger_overcharges(db)
    return found


async def _scan_spent_contradictions(db: AsyncSession, manager) -> int:
    """The sweep :func:`detect_spent_contradictions` throttles and guards. See it."""
    from backend.app.services import notify_dedup
    from backend.app.services.notification_service import notification_service
    from backend.app.services.spool_recovery import runout_slot_desc

    result = await db.execute(
        select(SpoolAssignment)
        .options(selectinload(SpoolAssignment.spool))
        .join(Spool, SpoolAssignment.spool_id == Spool.id)
        .where(Spool.spent_at.is_not(None), Spool.archived_at.is_(None))
    )
    found = 0
    for assignment in result.scalars().all():
        spool = assignment.spool
        if spool is None:
            continue
        try:
            state = manager.get_status(assignment.printer_id)
            tray = _resolve_live_tray(state, assignment.ams_id, assignment.tray_id)
            if not tray or tray_presence_from_dict(tray) is not True:
                continue  # gone or unknown — a binding whose roll left is a RELEASE question, not this one
            remain = _wire_remain_pct(tray)
            if remain is None or remain < _SPENT_CONTRADICTION_MIN_REMAIN_PCT:
                continue
            if not _binding_identity_holds(spool, tray):
                continue  # a DIFFERENT roll is in the slot — ordinary swap, nothing contradicted
            found += 1
            ledger_remaining = float(spool.label_weight or 0) - float(spool.weight_used or 0)
            logger.warning(
                "[RESPOOL] SPENT-CONTRADICTION: spool %d is stamped spent (%s) but is still seated on "
                "printer %d AMS%d-T%d reading %d%% full on the wire, while the ledger claims %.0f g of "
                "%.0f g left. A spent row is excluded from every print, so this roll is out of service. "
                "Nothing was changed — spent stamps are never cleared automatically.",
                spool.id,
                spool.spent_at.isoformat() if spool.spent_at else "?",
                assignment.printer_id,
                assignment.ams_id,
                assignment.tray_id,
                remain,
                ledger_remaining,
                float(spool.label_weight or 0),
            )
            key = f"spool:{spool.id}"
            last = await notify_dedup.last_sent_at(db, _SPENT_CONTRADICTION_SCOPE, key)
            if last is not None and (datetime.utcnow() - last).total_seconds() < _SPENT_CONTRADICTION_RENOTIFY_S:
                continue
            printer = await db.get(Printer, assignment.printer_id)
            global_tray = assignment.ams_id * 4 + assignment.tray_id
            await notification_service.on_spent_contradiction(
                assignment.printer_id,
                (printer.name if printer is not None else None) or f"Printer {assignment.printer_id}",
                spool.id,
                _spool_label(spool),
                runout_slot_desc(global_tray) or f"AMS{assignment.ams_id}-T{assignment.tray_id}",
                remain,
                db,
            )
            await notify_dedup.record_sent(db, _SPENT_CONTRADICTION_SCOPE, key)
        except Exception:  # noqa: BLE001 — one bad row must not abort the sweep
            logger.exception(
                "Spent-contradiction check failed for spool %s on printer %s AMS%s-T%s",
                spool.id,
                assignment.printer_id,
                assignment.ams_id,
                assignment.tray_id,
            )
    return found


def _spool_label(spool: Spool) -> str:
    """Short human name for a spool in operator copy ("Bambu Lab PETG Green")."""
    parts = [(spool.brand or "").strip(), (spool.material or "").strip(), (spool.color_name or "").strip()]
    return " ".join(p for p in parts if p) or f"spool #{spool.id}"


# --- Tier 2 / 3: automatic re-spool or prompt ------------------------------


def clear_respool_prompt_dedup(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Drop the cached per-slot prompt state (called when the slot reports empty).

    Clears ALL THREE per-slot memories in one place — the broadcast dedup tag, the
    tier-3 observation dedup (:data:`_respool_observation_logged`) and the
    remain-jump corroboration ledger (:data:`_jump_seen`). They share one lifetime:
    an emptied slot invalidates everything learned about the roll that was in it, so
    the next roll re-prompts, re-logs and re-corroborates from scratch. Keeping the
    set here means every caller of the empty edge (``main.on_ams_change``) and of a
    completed re-spool (:func:`respool_tag`) gets all of them without repeating
    itself.
    """
    for _dedup in (_respool_prompt_dedup, _respool_observation_logged):
        per_printer = _dedup.get(printer_id)
        if per_printer is not None:
            per_printer.pop((ams_id, tray_id), None)
    _jump_seen.pop((printer_id, ams_id, tray_id), None)


def respool_prompt_open_for_slot(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """True when a ``respool_prompt`` has been raised for this slot and not retired.

    Reads the same per-slot dedup :func:`pending_respool_prompts` snapshots from —
    an entry means "we asked the operator about the roll currently in this slot".
    It is deliberately the CHEAP, conservative reading: no DB round-trip and no
    re-validation, so a prompt the operator has since answered may still read open
    until the slot's empty edge clears the dedup.

    That bias is the correct one for its consumer. The tagged-ledger DECREASE
    reconcile defers to a pending prompt (the operator is being asked whether the
    tag moved onto a fresh roll — an automatic write-down would answer that
    question for them, and wrongly if a re-spool follows), and deferring one cycle
    too long costs nothing while writing one cycle too early costs the ledger.
    """
    return (ams_id, tray_id) in _respool_prompt_dedup.get(printer_id, {})


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

    # AMS live tray remain %, 1..100 or None — through the module's ONE parse
    # discipline, shared with the remain-jump margin and the contradiction detector.
    ams_remain_pct = _wire_remain_pct(tray)

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
        # Is this row's gram ledger PHYSICALLY IMPOSSIBLE (weight_used past the label)?
        # The Tier-3 branch already refuses to prompt on such a row, but the SPENT branch
        # must still ask — a spent+loaded spool deserves the question whatever the ledger
        # says — and it was asking it while quoting the garbage: prod prompts announced
        # "remaining −792.9 g". So the prompt stands and the NUMBERS are withdrawn. The
        # flag rides the payload rather than a pre-formatted string because the copy is
        # composed frontend-side and i18n'd there; `donor_remaining_g` and
        # `ledger_remain_pct` stay in the payload unchanged (the modal clamps them, and
        # dropping fields from a frozen contract would break the replay path).
        "ledger_unreliable": _ledger_corrupt(donor),
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
    # slot_pipeline.broadcast_unknown_tag so a failed push retries on the next tick).
    await ws_manager.broadcast(payload)
    per_printer[slot_key] = tag_key
    # An impossible ledger row must not have its "remaining" ASSERTED, here or in the
    # operator copy: a negative remaining is not a small number, it is a broken record,
    # and quoting it as fact is how the log stopped being triage evidence. State the
    # inputs instead, so the row can be found and repaired.
    remaining_desc = (
        f"remaining={payload['donor_remaining_g']:.1f}g"
        if not payload["ledger_unreliable"]
        else f"remaining=UNRELIABLE (used {float(donor.weight_used or 0):.1f}g of a "
        f"{float(donor.label_weight or 0):.1f}g label)"
    )
    logger.info(
        "respool_prompt broadcast: printer=%d AMS=%d slot=%d donor=%d %s trigger=%s",
        printer_id,
        ams_id,
        tray_id,
        donor.id,
        remaining_desc,
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


def remain_reading_untrustworthy(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """True while the tray's ``remain`` reading cannot be trusted for corroboration.

    A commanded identify in flight (the value is mid-re-read) or a drying unit (trays
    disengage and re-report) both produce transient tray payloads. Fails CLOSED
    (unknown → untrustworthy → no jump) and never raises, same contract as
    :func:`_swap_evidence`.

    Public because the tagged-ledger DECREASE reconcile in ``usage_tracker`` must
    apply the SAME trust rule as the re-spool trigger — a lane that wrote the
    ledger down from a mid-identify reading would be the very corruption both
    lanes exist to prevent.
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


def remain_jump_margin(spool: Spool, tray: dict) -> float | None:
    """Points-of-label by which the tray's WIRE remain% exceeds the ledger's.

    ``remain − (label_weight − weight_used) / label_weight × 100``, i.e. how far
    the firmware's reading of the physical roll disagrees with what the gram
    ledger claims is left, expressed in percentage points of the label weight.
    Positive = the wire says there is MORE filament than the ledger believes.

    ``None`` when the arithmetic does not apply at all: no valid RFID tag on the
    tray (an untagged roll has no wire truth to compare against), a non-positive
    label weight, or a ``remain`` that does not parse to an int in 1..100 (0 and
    −1 are firmware's "no reading", not "empty").

    THE one origin for that comparison. Two lanes consume it and must never drift
    apart: the re-spool trigger below (≥ :data:`_RESPOOL_REMAIN_JUMP_PCT`, a
    reused core carried the tag onto a fresh roll) and the tagged-ledger DECREASE
    reconcile in ``usage_tracker`` (≥ 50 points, doctrine rule 8 — for a tagged row
    the wire remain is truth and the ledger gets written DOWN).
    """
    if not is_valid_tag(tray.get("tag_uid", "") or "", tray.get("tray_uuid", "") or ""):
        return None
    label_weight = spool.label_weight or 0
    if label_weight <= 0:
        return None
    remain = _wire_remain_pct(tray)
    if remain is None:
        return None
    ledger_pct = max(0, label_weight - (spool.weight_used or 0)) / label_weight * 100
    return remain - ledger_pct


def _remain_jump_reading(spool: Spool, tray: dict) -> bool:
    """Detect a reused-core refill the gram ledger cannot see.

    A reused Bambu core carries its RFID tag onto a FRESH roll, so the firmware
    re-reads the tray as ~full (``remain`` ≈ 100%) while our ledger still holds the
    donor's near-spent ``weight_used``. The tag identity is CORRECT, so RFID
    re-reads never fix it — only a re-spool resets the ledger. True iff
    :func:`remain_jump_margin` is computable and at least
    :data:`_RESPOOL_REMAIN_JUMP_PCT`. A weight-locked fresh row (ledger ≈100%)
    cannot jump — ``remain`` cannot exceed 100 by 30 — so no special-case is
    needed for it.

    This is the INSTANTANEOUS reading only: pure arithmetic over one tray payload,
    no state, no trust check. The push-driven trigger consumes the corroborated
    :func:`_remain_jump` instead; the operator-initiated dismissal route (a single
    deliberate question about the live tray, with no push history to corroborate
    against) consumes this one.
    """
    margin = remain_jump_margin(spool, tray)
    return margin is not None and margin >= _RESPOOL_REMAIN_JUMP_PCT


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
    if remain_reading_untrustworthy(printer_id, ams_id, tray_id):
        return False
    now = _monotonic()
    first_seen, count = _jump_seen.get(key, (now, 0))
    count += 1
    _jump_seen[key] = (first_seen, count)
    return count >= _JUMP_MIN_PUSHES and (now - first_seen) >= _JUMP_STABLE_S


def should_evaluate_respool(spool: Spool, tray: dict, printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Single-origin gate for the existing-assignment respool call site.

    True when :func:`maybe_auto_or_prompt_respool` should run for a slot whose
    ``SpoolAssignment`` survived: either the row is a FINISHED roll
    (``Spool.is_finished_roll`` — the ONE encoding, read here as tier-2 evidence rather
    than as an exclusion) or the tray shows a CORROBORATED remain-jump refill the gram
    ledger missed (a Tier 3 trigger). Keeps the jump logic out of ``main.on_ams_change``
    so there is one definition; the slot coordinates are what let the jump corroborate
    per slot.
    """
    return spool.is_finished_roll or _remain_jump(spool, tray, printer_id, ams_id, tray_id)


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
    * Tier 2 (CONCLUDES, never asks): the row is a FINISHED roll
      (``Spool.is_finished_roll``) AND the tray is LOADED → the physical spool cannot be
      the spent one (you cannot add filament to a 0 g roll), so re-spool with the
      server-held last brand and return the NEW spool (the caller must skip its own
      auto-assign — the re-spool already re-assigned the slot). Since 2026-08-19 there is
      no ``respool_auto_enabled`` toggle in front of this: the conclusion follows from
      evidence (operator ruling 3). The prompt survives ONLY where the conclusion cannot
      be EXECUTED — no brand to mint with, or a sibling-tag conflict — which is an
      escalation, not an alternative verdict.
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

    if spool.is_finished_roll:
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
        # No ask here, and no toggle in front of it (operator ruling 3, 2026-08-19).
        # ``respool_auto_enabled`` used to gate this branch to OFF and prompt instead; it
        # encoded the superseded directive "the farm does NOT reuse tags yet", and the
        # ruling that superseded it is not a preference but a physical fact: you cannot
        # add filament to a 0 g roll, so a FINISHED roll reading LOADED can only be a new
        # roll on a reused core. That is CONCLUDED from evidence, never asked — the
        # setting was deleted rather than defaulted ON, because leaving it would leave a
        # dual path where a false spent stamp is the operator's problem to adjudicate on
        # every arrival. False stamps are root-caused at tier 1 (topology-gated since
        # 2026-08-09); the standing "Same spool" dismissal above is still the per-slot
        # brake, and it is checked BEFORE this point.
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
    # (2) The heuristic needs a REASON and EVIDENCE. The reason is the ledger reading
    # near-empty or a corroborated remain-jump (a reused core carried the tag onto a
    # fresh roll and the gram ledger never noticed). The evidence is physical: unless
    # somebody actually cycled a roll through this slot recently, the spool in it is
    # the same one the ledger already describes and there is nothing to observe.
    # This is what silences the standing near-empty rows — they are near-empty
    # because they were printed down, not because a roll was swapped.
    #
    # DEMOTED to an INFO line (autonomy ruling 2026-08-10). Tier 3 is a GUESS: it
    # has no hardware statement behind it, only grams and a timing window. Doctrine
    # rule 10 is now backed by the full-history audit — every respool prompt this
    # farm has ever raised was a false positive — so a tier-3 modal is, on this
    # fleet's evidence, always an interruption asking a human to adjudicate a
    # question the machine invented. Tier 2 (spent ∧ loaded — the firmware's own
    # exhaustion statement) is hardware-certain and still prompts, UNCHANGED.
    # Nothing is lost by logging instead: if the guess is ever right, the roll is a
    # fresh one on a reused tag and the operator's own re-spool action (tray menu)
    # is available whenever they see it — with this line in the log as the evidence
    # that the heuristic did fire, so a future re-promotion has real data to argue
    # from rather than an absence.
    remaining = (spool.label_weight or 0) - (spool.weight_used or 0)
    threshold = await _respool_prompt_threshold_g(db)
    near_empty = remaining <= threshold
    # `_remain_jump` mutates the cross-push corroboration ledger, so preserve the
    # original short-circuit exactly: it is consulted ONLY when near-empty did not
    # already answer, or its state machine would advance on pushes it never saw.
    jumped = False if near_empty else _remain_jump(spool, tray, printer_id, ams_id, tray_id)
    if not (near_empty or jumped):
        return None
    if not _swap_evidence(printer_id, ams_id, tray_id):
        logger.debug(
            "Re-spool heuristic withheld for spool %d (printer %d AMS%d-T%d): no physical roll cycle "
            "on the slot within %.0fs — an untouched spool cannot have become a fresh roll",
            spool.id,
            printer_id,
            ams_id,
            tray_id,
            _RESPOOL_SWAP_EVIDENCE_S,
        )
        return None
    # One line per (slot, tag), not one per push: the gate above can hold for the
    # whole swap-evidence window and this runs on the ~1 Hz status stream. Its own
    # dedup, deliberately NOT `_respool_prompt_dedup` — that dict is the record of
    # what the operator was ASKED, replayed to reconnecting clients by
    # `pending_respool_prompts`, and an entry there would resurrect as a prompt the
    # exact modal this demotion removes.
    slot_key = (ams_id, tray_id)
    tag_key = (tray.get("tag_uid") or "", tray.get("tray_uuid") or "")
    per_printer = _respool_observation_logged.setdefault(printer_id, {})
    if per_printer.get(slot_key) == tag_key:
        return None
    per_printer[slot_key] = tag_key
    logger.info(
        "Re-spool heuristic fired (observation only, no prompt): spool %d on printer %d AMS%d-T%d — "
        "reason=%s remaining=%.1fg of a %.1fg label (threshold %.1fg), AMS remain=%s%%, physical roll "
        "cycle seen within %.0fs. Tier 3 is a guess; if this roll really is a fresh one on a reused "
        "tag, re-spool it from the slot's tray menu.",
        spool.id,
        printer_id,
        ams_id,
        tray_id,
        "near_empty" if near_empty else "remain_jump",
        float(remaining),
        float(spool.label_weight or 0),
        float(threshold),
        tray.get("remain"),
        _RESPOOL_SWAP_EVIDENCE_S,
    )
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
    #
    # DELIBERATELY compares ``tag_uid`` ALONE — do NOT "unify" this onto
    # ``tag_matches_row``. Every other tag comparison in the fork asks "is this the same
    # roll?" and wants the sibling folded in; this one asks the opposite question, "did
    # the donor's OTHER chip just arrive?", and the tag DISAGREEMENT is precisely the
    # signal it fires on. Routing it through the pair comparer would make a recorded
    # sibling match, the disagreement vanish, and this 409 silently stop existing —
    # letting one donor roll's two chips each spawn a re-spooled row (one tag per donor).
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
    # this very ROLL (double-submit, or the auto path racing a manual confirm) —
    # disposing it and minting another would churn a duplicate. Return it
    # unchanged; a brand correction goes through the normal spool edit.
    #
    # Unlike the sibling GUARD above, this asks the ordinary "same roll?" question, so
    # it matches EITHER recorded chip: a re-submit that happens to scan the fresh row's
    # far side is the same double-submit, and comparing the near chip alone let it fall
    # through to a second full re-spool of a row already re-spooled.
    if (
        donor is not None
        and donor.tag_type == RESPOOL_TAG_TYPE
        and donor.spent_at is None
        and not (donor.weight_used or 0)
        and norm_uid
        and tag_matches_row(
            norm_uid,
            normalize_tag_uid(donor.tag_uid or ""),
            normalize_tag_uid(donor.sibling_tag_uid or ""),
        )
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
