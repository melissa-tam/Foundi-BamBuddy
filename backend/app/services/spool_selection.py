"""Spool-selection policy owner for farm dispatch.

Single source of truth for *which loaded AMS spool starts a print* when more
than one tray satisfies a filament requirement. Extracted from
``print_scheduler`` so the policy lives in one testable place and the scheduler
keeps thin delegating methods.

Three policies (``SELECTION_POLICIES``), chosen by the ``spool_selection_policy``
setting:

* ``slot_order`` — legacy AMS-slot emission order (no sort).
* ``lowest_remaining`` — prefer the most-spent matching spool. Gated on the
  printer's AMS Filament Backup so we never sort toward a near-empty spool the
  printer can't switch away from (#1766 — see :func:`effective_policy`).
* ``first_loaded`` (farm default) — FIFO by time-in-AMS: the roll that has been
  seated longest is drained first. The ordinal is when the roll CURRENTLY in the
  tray became seated (``Spool.loaded_at``, re-stamped on a binding change or a
  never-fed re-seat; falls back to the write-once ``first_loaded_at`` then
  ``created_at`` / Spoolman ``first_used``) — NOT the bound ledger row's age.
  006-H2S proved the row-age reading let a stale binding lend its age to a fresh
  roll and invert FIFO. When AMS Backup can't cover a mid-print switch, a
  *smart-cover* partition keeps a candidate that can finish the job on its own
  ahead of an older one that would run dry.

Plus a minimum-start-weight rule (``min_start_spool_g``): a spool that cannot be
PROVEN to hold at least the floor can never be the STARTING spool of a print (it
stays a firmware backup donor). A candidate fails that proof two ways, kept
distinct as :data:`START_BLOCK_BELOW_FLOOR` / :data:`START_BLOCK_UNKNOWN_GRAMS`:
its known remaining is under the floor, or its remaining is UNKNOWN (an unbound
tray, or a binding whose grams nothing can quote). Unknown fails CLOSED under
``require_known_grams`` — a roll the ledger cannot price may hold 5 g, and an
unpriceable candidate used to sit in ``eligible`` and start prints (prod trace
2026-08-07 23:38:13, ``inv_g=None`` beside ``remain=46``). That reading is scoped
to printers whose ledger demonstrably speaks: with NOT ONE loaded slot priced,
the install keeps no spool inventory and every roll would be refused forever, so
the unknown reserve stands down (the same stance the deficit lane already takes
on undetermined grams). A gap in a ledger that IS in use is the closed hole.

Fail-closed is the START reading only. ``spool_recovery``'s mid-print donor
search leaves ``require_known_grams`` off by design: a refill runs against a 5 g
hard floor to keep a live print moving, where refusing an unpriceable roll would
stall the print it exists to rescue. Two consequences of the start reading are
deliberate, both on a printer whose ledger speaks: an externally-held roll
(``vt_tray``) is never inventory-tracked, so it is unpriceable by construction and
cannot START a print, and a tray with no binding at all is likewise start-blocked.
Both remain startable via "Print Anyway" (``skip_filament_check`` → floor 0) or
``min_start_spool_g = 0``, which disable the floor and every reading of it at once.

When the only otherwise-matching spool fails the proof, the requirement's slot is
reported in :attr:`MatchOutcome.start_blocked_slots` (with its kind in
``start_block_kinds``) so the caller can stage the job with a distinct reason
instead of silently dispatching or falling back to a mismatch.

Above every policy sits a hard exclude of unusable spools: a spool flagged with a
mid-print feed fault (``Spool.feed_fault_at`` → :attr:`SlotInventory.out_of_rotation`),
a spent spool (``Spool.spent_at`` → :attr:`SlotInventory.spent`), or an ARCHIVED row
(``Spool.archived_at`` → :attr:`SlotInventory.archived`) is removed from the candidate
set before any eligibility split, so it can never start a print, be staged, or surface
in ``start_blocked_slots`` — it is simply invisible to selection until the condition
clears. This exclusion is unconditional: a jammed, spent or retired spool never starts
a print regardless of the selection policy or the minimum-start floor.

A fourth exclusion is a WIRE fact rather than a ledger one: a SEATED-but-UNIDENTIFIED
tray (:attr:`SlotInventory.unread` / the ``unread`` key the scheduler's loaded-filament
extraction now stamps). Such a slot physically holds a roll the farm cannot name, so it
can neither be matched (no material to match on) nor priced (its binding, if any, is a
hypothesis). It is excluded BY NAME rather than by the accident of carrying an empty
filament type, so the reason a phantom "no match" happened is visible in the decision
trace and a future builder change cannot silently make unidentified rolls selectable.
The dispatch answer for such a slot is not "stage the job" but "read the slot" —
``waiting_reason`` :data:`WAITING_REASON_UNREAD_PENDING`, driven by the scheduler.

Finally, an operator may PIN a requirement slot to one specific tray
(``PrintQueueItem.ams_mapping``, position = slot_id - 1). A pin is an INSTRUCTION,
never a cached derivation: it narrows that requirement's candidate set to the pinned
``global_tray_id`` and then every rule above applies to it unchanged — the unusable
excludes, the nozzle filter and the minimum-start floor included. Only the bucket
scan is skipped, because a pin already names the tray and filament TYPE is
deliberately not enforced on it (an explicit cross-type pick is a ratified operator
override, #1722). A pinned tray that is not among the live candidates leaves its slot
unmatched under :data:`WAITING_REASON_PINNED_UNAVAILABLE` — a pin miss is a missing
roll, not "low filament", and the two need different operator actions.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.utils.filament_types import canonical_filament_type

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.models.print_queue import PrintQueueItem

logger = logging.getLogger(__name__)

# Public policy surface. Keep in lock-step with AppSettings.spool_selection_policy
# / min_start_spool_g (guarded by test_spool_selection.py's defaults-drift test).
SELECTION_POLICIES: tuple[str, ...] = ("slot_order", "lowest_remaining", "first_loaded")
DEFAULT_SELECTION_POLICY = "first_loaded"
DEFAULT_MIN_START_SPOOL_G = 150
# Machine waiting_reason token for a job held because its starting spool is below
# the minimum-start floor. Rendered by QueuePage; released by farm_staging.
WAITING_REASON_START_MIN = "start_spool_below_minimum"
# Machine waiting_reason token for a job whose filament requirement is uncovered by the
# IDENTIFIED slots while the candidate printer still has ≥1 seated-but-unidentified tray
# (:attr:`SlotInventory.unread`). NOT a staging reason: the item stays pending and
# un-promoted, the deficit lane asks the printer to read the slot, and the next tick
# re-decides on real evidence. Self-clearing exactly like ``stagger_hold`` /
# ``no_usb_drive`` — dispatch clears it, and a genuine deficit overwrites it.
WAITING_REASON_UNREAD_PENDING = "filament_unread_pending"
# Machine waiting_reason token for a job whose operator PIN (an explicit
# ``ams_mapping`` entry) names a tray that is not among the printer's live
# candidates — pulled, never loaded, seated-but-unread, out of rotation/spent, or on
# the wrong nozzle. Deliberately NOT a "Low filament" staging string: nothing is
# short, a NAMED roll is simply not there, and the operator action is "load that tray
# or drop the pin" rather than "top a spool up". Self-clearing like the tokens above —
# the matcher re-decides every tick, so loading the tray dispatches the job with no
# further human step. Also the ``POST /queue/{id}/start`` 409 code (mirroring
# :data:`WAITING_REASON_START_MIN`), so the ▶ button explains itself instead of
# leaving the item silently pending.
WAITING_REASON_PINNED_UNAVAILABLE = "pinned_tray_unavailable"

# Why a slot could not be started. ONE vocabulary, cited by the decision trace,
# the staged waiting_reason (``farm_staging.build_staged_reason``) and the tests —
# the two failures need different operator actions (top the roll up vs. give the
# farm a weight for it), so they must stay tellable apart in triage.
START_BLOCK_BELOW_FLOOR = "below_floor"
START_BLOCK_UNKNOWN_GRAMS = "unknown_grams"


def dominant_start_block(kinds: Iterable[str]) -> str | None:
    """Collapse start-block kinds into the ONE kind that words a single hold.

    ``unknown_grams`` only when EVERY block is unknown-grams: a mixed set contains
    at least one roll the farm HAS priced below the floor, and "below minimum" is
    the true statement about that roll. Empty ⇒ ``None`` (nothing is blocked).
    Used per-slot by the matcher, per-outcome by :attr:`MatchOutcome.start_block_kind`,
    and across candidate printers by the scheduler's model lane, so the rule that
    decides the wording lives in exactly one place.
    """
    ks = set(kinds)
    if not ks:
        return None
    return START_BLOCK_UNKNOWN_GRAMS if ks == {START_BLOCK_UNKNOWN_GRAMS} else START_BLOCK_BELOW_FLOOR


@dataclass
class SlotInventory:
    """Per-slot inventory facts used by the selection policies.

    ``remaining_g`` is the operator's authoritative remaining weight (Bambuddy
    :attr:`Spool.remaining_g` or Spoolman ``remaining_weight``); ``None``
    when the slot has no inventory binding (the sort then falls back to the MQTT
    ``remain`` percentage). ``first_loaded_ord`` is the FIFO ordinal in epoch
    seconds — time-in-AMS, i.e. when the roll currently in the tray last became
    seated: ``COALESCE(loaded_at, first_loaded_at, created_at)`` internally
    (Spoolman: ``first_used``); ``None`` when unknown/unbound. ``ord_src`` names
    which source won (``"loaded_at"`` = a genuine reseat/binding stamp, else a
    stale-ledger fallback ``"first_loaded_at"``/``"created_at"``, or ``"first_used"``
    in Spoolman mode) so the ``[spool-select]`` trace shows why a slot sorts where it
    does.

    ``out_of_rotation`` is the feed-fault hard-exclude flag: a spool flagged with a
    mid-print feed fault (jam / tangle) is out of service and must never be selected.
    ``spent`` is the run-dry hard-exclude flag: a spool marked spent has no filament
    left to start with. ``archived`` is the retired-row hard-exclude flag: an
    archived spool has been taken out of inventory by an operator or by a donor
    disposal, and an archived-but-still-bound row was a live START candidate until
    2026-08-02 (repairs that archived a rotten row without rebinding the slot left
    it selectable). All three are kept SEPARATE (their log/operator semantics
    differ) and all three hard-exclude the slot. Only the internal inventory mode
    can set them (``Spool.feed_fault_at`` / ``spent_at`` / ``archived_at``);
    Spoolman has no such concept, so they stay ``False`` there.

    ``unread`` is the SEATED-but-UNIDENTIFIED flag (``tray_fields.tray_unread`` applied
    to the live tray by the scheduler's loaded-filament extraction, carried here on the
    loaded entry). It is a WIRE fact, not a ledger one, and it means the slot holds a
    roll the farm cannot name — so the binding's grams are a hypothesis and
    ``remaining_g`` is forced to ``None`` (undetermined) for such a row rather than
    quoting a ledger figure nothing has verified. Both modes can set it.
    """

    remaining_g: float | None
    first_loaded_ord: float | None
    ord_src: str | None = None
    out_of_rotation: bool = False
    spent: bool = False
    archived: bool = False
    unread: bool = False


@dataclass
class MatchOutcome:
    """Result of matching required filaments to loaded slots.

    ``mapping`` is the AMS mapping array (position = slot_id - 1, value =
    global_tray_id or -1), or ``None`` when nothing to map.

    ``start_block_kinds`` maps slot_id → ``START_BLOCK_*`` for every slot that had
    NO eligible spool solely because each otherwise-matching candidate failed the
    minimum-start proof (a reserved candidate WOULD have matched) — the distinct
    "cannot start on this" signal. It is THE start-block record: the
    ``start_blocked_slots`` list is its key view, so the slot set and the reasons
    can never disagree.

    ``pin_missing`` maps slot_id → the pinned ``global_tray_id`` for every slot whose
    operator PIN named a tray the printer is not offering (see
    :data:`WAITING_REASON_PINNED_UNAVAILABLE`). Kept apart from ``start_block_kinds``
    because the two hold for opposite reasons — the roll is absent vs. the roll is
    present but unstartable — and word different operator instructions.

    ``unmatched_slots`` is the TOTAL-OUTCOME record: every requirement slot the
    matcher could not resolve, whatever the cause (pin miss, start-block, or simply
    no matching roll). It is derived from the REQUIREMENTS, never from scanning the
    mapping array for ``-1``: requirement slot_ids can be sparse (a plate using only
    filament 2 yields ``[-1, gtid]``), so a mapping scan reports a phantom hole for a
    slot nothing ever asked about. Dispatch reads :attr:`is_total` — an item may not
    start while ANY requirement is unresolved, because the alternative is a partial
    ``-1`` mapping that means "nothing feeds this extruder" on the wire.
    """

    mapping: list[int] | None
    start_block_kinds: dict[int, str] = field(default_factory=dict)
    pin_missing: dict[int, int] = field(default_factory=dict)
    unmatched_slots: tuple[int, ...] = ()

    @property
    def start_blocked_slots(self) -> list[int]:
        """Slot_ids held back by the minimum-start proof (insertion-ordered)."""
        return list(self.start_block_kinds)

    @property
    def start_block_kind(self) -> str | None:
        """The one kind that words a hold covering this outcome (see
        :func:`dominant_start_block`); ``None`` when nothing is start-blocked."""
        return dominant_start_block(self.start_block_kinds.values())

    @property
    def pinned_unavailable_slots(self) -> list[int]:
        """Slot_ids whose pinned tray is not among the live candidates."""
        return list(self.pin_missing)

    @property
    def is_total(self) -> bool:
        """True when every requirement resolved to a tray — the dispatch contract.

        A mapping-free outcome (``mapping is None``: no requirements to map, no live
        status, nothing loaded) is total by construction — there is no unresolved
        requirement in it. That is the legitimate mapping-free dispatch the fork has
        always allowed, and it must never be confused with a hole.
        """
        return not self.unmatched_slots


# ---------------------------------------------------------------------------
# Colour helpers (canonical home; PrintScheduler delegates here). Kept module
# level so both the matcher and the scheduler's force-colour paths share one
# implementation.
# ---------------------------------------------------------------------------
def normalize_color_for_compare(color: str | None) -> str:
    """Normalize a colour for comparison (lowercase, no hash, RGB only)."""
    if not color:
        return ""
    return color.replace("#", "").lower()[:6]


def colors_are_similar(color1: str | None, color2: str | None, threshold: int = 40) -> bool:
    """True if two colours are within ``threshold`` on every RGB channel."""
    hex1 = normalize_color_for_compare(color1)
    hex2 = normalize_color_for_compare(color2)
    if not hex1 or not hex2 or len(hex1) < 6 or len(hex2) < 6:
        return False
    try:
        r1, g1, b1 = int(hex1[0:2], 16), int(hex1[2:4], 16), int(hex1[4:6], 16)
        r2, g2, b2 = int(hex2[0:2], 16), int(hex2[2:4], 16), int(hex2[4:6], 16)
    except ValueError:
        return False
    return abs(r1 - r2) <= threshold and abs(g1 - g2) <= threshold and abs(b1 - b2) <= threshold


# ---------------------------------------------------------------------------
# Slot ordering
# ---------------------------------------------------------------------------
def slot_priority(ams_id: int | None, tray_id: int | None) -> int:
    """Deterministic slot-position tie-breaker.

    Three bands so regular AMS < AMS-HT < external on ties, regardless of the
    raw ``ams_id`` (in particular external/VT ``ams_id = -1`` must NOT sort to a
    negative number and beat AMS slot 0):

    - Regular AMS (``ams_id`` 0..7): ``ams_id * 4 + tray_id`` → 0..31
    - AMS-HT (``ams_id`` >= 128, single tray): ``1000 + (ams_id - 128) * 4``
    - External / VT (``ams_id`` < 0 or ``None``): ``10_000``
    """
    if ams_id is None or ams_id < 0:
        return 10_000
    if ams_id >= 128:
        return 1_000 + (ams_id - 128) * 4 + (tray_id or 0)
    return ams_id * 4 + (tray_id or 0)


def effective_policy(policy: str | None, ams_filament_backup: bool | None) -> str:
    """Resolve the runtime policy, applying the #1766 AMS-Backup gate.

    ``lowest_remaining`` requires the printer to be able to switch to a backup
    spool when the picked one runs out; with backup explicitly OFF, sorting
    toward the lowest would strand the print, so it degrades to ``slot_order``.
    ``None`` (unknown / A1 family) preserves the requested policy. ``first_loaded``
    and ``slot_order`` pass through (first_loaded's backup handling is the
    smart-cover partition in :func:`match_filaments_to_slots`). An unknown policy
    string falls back to the default.
    """
    if policy not in SELECTION_POLICIES:
        return DEFAULT_SELECTION_POLICY
    if policy == "lowest_remaining" and ams_filament_backup is False:
        return "slot_order"
    return policy


def _lowest_remaining_key(f: dict, inv: dict[int, SlotInventory] | None) -> tuple[int, float, int]:
    """Two-tier sort key: inventory-tracked spools before MQTT-only ones, then
    ascending by remaining, then slot position. Grams vs percent never compare
    because the tier flag dominates; unknown MQTT ``remain`` maps to 101."""
    gtid = f.get("global_tray_id")
    prio = slot_priority(f.get("ams_id"), f.get("tray_id"))
    si = inv.get(gtid) if inv else None
    if si is not None and si.remaining_g is not None:
        return (0, si.remaining_g, prio)
    remain = f.get("remain", -1)
    return (1, float(remain) if remain is not None and remain >= 0 else 101.0, prio)


def _first_loaded_key(f: dict, inv: dict[int, SlotInventory] | None) -> tuple[int, float, int]:
    """FIFO sort key: spools with a known first-loaded ordinal first (ascending,
    oldest first), unbound trays last, slot position as the final tie-break."""
    gtid = f.get("global_tray_id")
    prio = slot_priority(f.get("ams_id"), f.get("tray_id"))
    si = inv.get(gtid) if inv else None
    if si is not None and si.first_loaded_ord is not None:
        return (0, si.first_loaded_ord, prio)
    return (1, 0.0, prio)


def _sort_candidates(candidates: list[dict], policy: str, inv: dict[int, SlotInventory] | None) -> None:
    """Sort ``candidates`` in place by the policy key. ``slot_order`` is a no-op
    (preserves emission order)."""
    if policy == "lowest_remaining":
        candidates.sort(key=lambda f: _lowest_remaining_key(f, inv))
    elif policy == "first_loaded":
        candidates.sort(key=lambda f: _first_loaded_key(f, inv))


def _known_remaining(f: dict, inv: dict[int, SlotInventory] | None) -> float | None:
    """Inventory-known remaining grams for a slot, else ``None`` (unknown/unbound
    — the MQTT ``remain`` percentage is NOT used here because it isn't grams)."""
    si = inv.get(f.get("global_tray_id")) if inv else None
    if si is not None and si.remaining_g is not None:
        return si.remaining_g
    return None


def _covers(f: dict, inv: dict[int, SlotInventory] | None, used_grams: float | None) -> bool:
    """True if ``f`` can supply the whole requirement on its own. Unknown
    remaining (or unknown requirement) ⇒ assumed covering."""
    if used_grams is None:
        return True
    rem = _known_remaining(f, inv)
    if rem is None:
        return True
    return rem >= used_grams


def filament_type_of(entry: dict) -> str:
    """The upper-cased filament type of a requirement OR candidate dict.

    ONE origin for reading the ``type`` key off either side of a comparison (both
    shapes carry it under the same name), so the matcher's comparison casing, the
    decision-trace log and any new caller can never drift apart.
    """
    return (entry.get("type") or "").upper()


def filament_types_match(a: dict, b: dict) -> bool:
    """True when two requirement/candidate dicts name the SAME canonical filament type.

    THE type-equality rule — canonicalised on both sides (``PLA Basic`` ≡ ``PLA``),
    so equivalent trade names never read as a mismatch. Extracted from the three
    inlined compares this module used to carry; every type comparison, existing or
    new, goes through here.
    """
    return canonical_filament_type(filament_type_of(a)) == canonical_filament_type(filament_type_of(b))


def parse_pins(raw: str | None) -> list[int] | None:
    """Parse a stored ``PrintQueueItem.ams_mapping`` JSON string into a pin array.

    ONE origin for turning the stored TEXT column into the matcher's ``pins`` input.
    Anything that is not a JSON list of ints reads as "no instruction" (``None``)
    rather than raising: a malformed field must never wedge dispatch, and the
    schema-level validator (``schemas/print_queue``) is what keeps well-formed values
    well-formed at the API boundary.
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    return [v if isinstance(v, int) and not isinstance(v, bool) else -1 for v in parsed]


def _pin_for(pins: list[int] | None, slot_id: int) -> int | None:
    """The operator's explicit tray pin for ``slot_id``, or ``None`` for "no pin".

    ``pins`` is the stored instruction array (position = slot_id - 1). A negative
    entry (``-1``), a short array, a non-int and a bool (``True`` is an ``int``
    subclass, and a boolean is never a tray id) all mean the same thing: this slot
    carries no instruction and auto-matches.
    """
    if not pins or slot_id <= 0 or slot_id > len(pins):
        return None
    value = pins[slot_id - 1]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _scan_candidates(req: dict, candidates: list[dict]) -> dict | None:
    """Bucket-precedence match over a pre-sorted, nozzle-filtered, not-yet-used
    candidate list: unique tray_info_idx > exact colour > similar colour >
    type-only. Returns the chosen loaded-filament dict, or ``None``."""
    req_color = req.get("color", "")
    req_tray_info_idx = req.get("tray_info_idx", "")

    idx_match = exact_match = similar_match = type_only_match = None

    if req_tray_info_idx:
        idx_matches = [f for f in candidates if f.get("tray_info_idx") == req_tray_info_idx]
        if len(idx_matches) == 1:
            idx_match = idx_matches[0]
        elif len(idx_matches) > 1:
            # Multiple trays share the preset id — colour-match within the subset
            # (already policy-sorted, so filtering keeps the intended order).
            for f in idx_matches:
                f_color = f.get("color", "")
                if normalize_color_for_compare(f_color) == normalize_color_for_compare(req_color):
                    if not exact_match:
                        exact_match = f
                elif colors_are_similar(f_color, req_color):
                    if not similar_match:
                        similar_match = f
                elif not type_only_match:
                    type_only_match = f

    if not idx_match and not exact_match and not similar_match and not type_only_match:
        for f in candidates:
            if not filament_types_match(f, req):
                continue
            f_color = f.get("color", "")
            if normalize_color_for_compare(f_color) == normalize_color_for_compare(req_color):
                if not exact_match:
                    exact_match = f
            elif colors_are_similar(f_color, req_color):
                if not similar_match:
                    similar_match = f
            elif not type_only_match:
                type_only_match = f

    return idx_match or exact_match or similar_match or type_only_match


def match_filaments_to_slots(
    required: list[dict],
    loaded: list[dict],
    *,
    policy: str,
    inv: dict[int, SlotInventory] | None,
    backup_on: bool | None,
    min_start_g: int,
    require_known_grams: bool = False,
    pins: list[int] | None = None,
) -> MatchOutcome:
    """Match required filaments to loaded slots under the given policy.

    ``pins`` is the operator's instruction array (``PrintQueueItem.ams_mapping``,
    position = slot_id - 1; ``-1``/absent = no pin). It is an INPUT to this one
    matcher, never a parallel path: a pinned requirement's candidate set is narrowed
    to that single ``global_tray_id`` and then walks the SAME rules as any other —
    the unusable hard-excludes, the nozzle filter and the minimum-start floor all
    apply unchanged, so a pin can no more start a jammed, spent or below-floor roll
    than an auto-match can (``skip_filament_check`` → ``min_start_g = 0`` remains the
    one sanctioned override, and it works by disabling the floor for everyone).
    Only the bucket scan is skipped: the pin already names the tray, and filament
    TYPE is deliberately NOT enforced on it — the dialog shows the comparison and an
    explicit cross-type pick is the operator's ratified override (#1722). A pinned
    tray absent from the live candidates records the slot in
    :attr:`MatchOutcome.pin_missing` and leaves it unmatched.

    ``require_known_grams`` is the START reading of the floor: a candidate whose
    remaining grams are UNKNOWN is reserved instead of started, because an
    unpriceable roll may hold 5 g. Dispatch / manual-start / preview all pass it;
    the default is OFF for ``spool_recovery``'s mid-print donor search, which must
    keep a live print fed against a 5 g hard floor (see the module docstring). It
    takes effect only when at least one loaded slot HAS quotable grams — a printer
    with no inventory at all is not tracking spools, and a floor that refuses every
    roll would park it rather than protect it.

    Bucket precedence (unique tray_info_idx > exact colour > similar colour >
    type-only) and the nozzle filter are UNCHANGED from the legacy matcher. On
    top, per requirement over the not-yet-used candidates:

    0. Unusable candidates are hard-excluded up front, so a jammed or spent spool
       never starts a print regardless of the policy or the floor: out-of-rotation
       (``SlotInventory.out_of_rotation`` — a jammed / feed-fault spool) and spent
       (``SlotInventory.spent`` — a run-dry spool). They enter neither ``eligible``
       nor ``dropped``, so they can never match nor appear in ``start_blocked_slots``.
       An UNREAD candidate (``loaded`` entry stamped ``unread`` — a seated tray with no
       identity at all) is excluded FIRST and independently of ``inv``, because that
       exclusion is a wire fact that holds whether or not the slot carries a binding.
    1. ``min_start_g > 0`` reserves candidates that fail the minimum-start proof:
       known remaining below the floor, and — under ``require_known_grams`` —
       unknown remaining. Each reserve keeps its own list so the block it causes
       can name its kind. ``min_start_g == 0`` disables the floor and BOTH
       readings of it (that is what "Print Anyway" leans on).
    2. Eligible candidates are sorted by the policy key (``slot_order`` = none).
    3. ``first_loaded`` with backup not ON stable-partitions eligible into
       covering-first (a candidate covers when its remaining is unknown or
       >= the requirement's ``used_grams``), FIFO within each half.
    4. The bucket scan runs on eligible (a PINNED slot takes its single surviving
       candidate directly — see ``pins``); on a miss, a reserved candidate that
       WOULD have matched records the slot start-blocked under its reserve's kind,
       and a pin whose tray never reached the candidate set records ``pin_missing``.
    """
    if not required:
        return MatchOutcome(mapping=None)

    # The START reading applies only where the ledger can speak. When NOT ONE loaded
    # slot has quotable grams, this printer keeps no inventory at all (an install that
    # does not use the spool module, a freshly-onboarded printer, a repair that left
    # every slot unbound) and the floor has nothing to say about any of them: refusing
    # every roll would park the printer instead of protecting it, and the deficit lane
    # already treats undetermined grams as non-blocking for the same reason. The shape
    # this closes is one unpriced roll BESIDE priced ones — a gap in a ledger that is
    # demonstrably in use, which is what the 2026-08-07 23:38:13 trace showed.
    ledger_priced = any(si.remaining_g is not None for si in inv.values()) if inv else False
    reserve_unknown = require_known_grams and ledger_priced

    trace = policy != "slot_order" or min_start_g > 0 or bool(pins)
    used_tray_ids: set[int] = set()
    comparisons: list[dict] = []
    start_block_kinds: dict[int, str] = {}
    pin_missing: dict[int, int] = {}
    unmatched_slots: list[int] = []

    for req in required:
        slot_id = req.get("slot_id", 0)
        available = [f for f in loaded if f["global_tray_id"] not in used_tray_ids]

        # Operator PIN: narrow this requirement to the one named tray BEFORE every
        # other rule, so the pinned candidate is judged by exactly the rules an
        # auto-matched one would face (excludes → nozzle → floor). The narrowing runs
        # over the not-yet-used set, so two slots pinned to the SAME tray behave like
        # any other double claim: the first takes it and the second reports its pin
        # unavailable rather than silently feeding one roll to two requirements.
        pin = _pin_for(pins, slot_id)
        if pin is not None:
            available = [f for f in available if f.get("global_tray_id") == pin]

        # (0) Unusable-spool hard exclude: a jammed / feed-fault spool
        # (out_of_rotation), a spent spool, or an ARCHIVED (retired) row leaves the
        # candidate set entirely BEFORE any eligibility split, so none can ever
        # start a print, be staged, or surface in start_blocked_slots — regardless
        # of the policy or the minimum-start floor. The three reasons are kept
        # SEPARATE so the trace names why a slot vanished (jam vs run-dry vs
        # retired-row differ operationally).
        # Seated-but-UNIDENTIFIED trays leave first, and WITHOUT consulting ``inv``: an
        # unread slot may be unbound entirely (the 12-tray fleet reality), so keying the
        # exclusion off an inventory row would miss exactly the slots that need it. The
        # candidate carries the wire verdict on itself (``tray_fields.tray_unread``,
        # stamped by the scheduler's loaded-filament extraction). Excluding by NAME
        # rather than relying on the empty filament type to fail every bucket keeps the
        # reason visible in the trace and survives a future builder change.
        excluded_unread: list[int] = [f["global_tray_id"] for f in available if f.get("unread")]
        if excluded_unread:
            available = [f for f in available if not f.get("unread")]

        excluded_oor: list[int] = []
        excluded_spent: list[int] = []
        excluded_archived: list[int] = []
        if inv:
            kept: list[dict] = []
            for f in available:
                si = inv.get(f["global_tray_id"])
                if si is not None and si.out_of_rotation:
                    excluded_oor.append(f["global_tray_id"])
                elif si is not None and si.spent:
                    excluded_spent.append(f["global_tray_id"])
                elif si is not None and si.archived:
                    excluded_archived.append(f["global_tray_id"])
                else:
                    kept.append(f)
            available = kept

        # Nozzle-aware hard filter (cross-nozzle assignment fails the print).
        req_nozzle_id = req.get("nozzle_id")
        if req_nozzle_id is not None:
            available = [f for f in available if f.get("extruder_id") == req_nozzle_id]

        # (1) minimum-start floor: reserve every candidate that cannot be PROVEN to
        # clear it. Known-low rolls stay backup donors; unknown-grams rolls are
        # reserved only under the START reading (``require_known_grams``) — the
        # mid-print donor path must still be able to feed a print from a roll the
        # ledger cannot price. The two reserves stay separate so a block names WHY.
        eligible: list[dict] = []
        dropped_low: list[dict] = []
        dropped_unknown: list[dict] = []
        if min_start_g > 0:
            for f in available:
                rem = _known_remaining(f, inv)
                if rem is None:
                    (dropped_unknown if reserve_unknown else eligible).append(f)
                elif rem < min_start_g:
                    dropped_low.append(f)
                else:
                    eligible.append(f)
        else:
            eligible = list(available)

        # (2) policy sort.
        _sort_candidates(eligible, policy, inv)

        # (3) first_loaded smart-cover partition when backup can't rescue a switch.
        if policy == "first_loaded" and backup_on is not True:
            used_grams = req.get("used_grams")
            covering = [f for f in eligible if _covers(f, inv, used_grams)]
            non_covering = [f for f in eligible if not _covers(f, inv, used_grams)]
            eligible = covering + non_covering

        if trace:
            logger.info(
                "[spool-select] slot=%s pin=%s type=%r color=%r tii=%r nozzle=%s policy=%s min_start=%s "
                "require_known_grams=%s ledger_priced=%s; eligible=%s dropped_below_floor=%s "
                "dropped_unknown_grams=%s excluded_oor=%s excluded_spent=%s excluded_archived=%s "
                "excluded_unread=%s",
                slot_id,
                pin,
                filament_type_of(req),
                req.get("color", ""),
                req.get("tray_info_idx", ""),
                req_nozzle_id,
                policy,
                min_start_g,
                require_known_grams,
                ledger_priced,
                _trace_rows(eligible, inv),
                _trace_rows(dropped_low, inv),
                _trace_rows(dropped_unknown, inv),
                excluded_oor,
                excluded_spent,
                excluded_archived,
                excluded_unread,
            )

        # (4) selection. A PINNED slot takes its single surviving candidate outright:
        # the pin already named the tray, so the bucket scan (which would re-impose a
        # TYPE match) is deliberately not consulted — #1722, an explicit cross-type
        # pick is the operator's call. Everything that could disqualify the roll has
        # already run above. Unpinned slots keep the bucket precedence unchanged.
        if pin is not None:
            match = eligible[0] if eligible else None
        else:
            match = _scan_candidates(req, eligible)
        if match is None:
            if pin is not None:
                # The pinned roll IS on the printer but failed the minimum-start
                # proof: that is a start-block, worded by the floor's own vocabulary,
                # NOT a missing tray. At most one candidate can survive the narrowing,
                # so at most one reserve holds it.
                kind = dominant_start_block(
                    ({START_BLOCK_BELOW_FLOOR} if dropped_low else set())
                    | ({START_BLOCK_UNKNOWN_GRAMS} if dropped_unknown else set())
                )
                if kind is not None:
                    start_block_kinds[slot_id] = kind
                else:
                    pin_missing[slot_id] = pin
                    if trace:
                        logger.info(
                            "[spool-select] slot=%s PINNED TRAY UNAVAILABLE gtid=%s — the pinned tray is not "
                            "among this printer's live candidates (not loaded, unread, out of rotation/spent, "
                            "on another nozzle, or already taken by an earlier slot)",
                            slot_id,
                            pin,
                        )
            else:
                matched_kinds: set[str] = set()
                for reserve, reserve_kind in (
                    (dropped_low, START_BLOCK_BELOW_FLOOR),
                    (dropped_unknown, START_BLOCK_UNKNOWN_GRAMS),
                ):
                    if not reserve:
                        continue
                    _sort_candidates(reserve, policy, inv)
                    if _scan_candidates(req, reserve) is not None:
                        matched_kinds.add(reserve_kind)
                kind = dominant_start_block(matched_kinds)
                if kind is not None:
                    start_block_kinds[slot_id] = kind
            if slot_id in start_block_kinds and trace:
                logger.info(
                    "[spool-select] slot=%s START-BLOCKED reason=%s — no match can be proven to clear "
                    "the %s g floor (below-floor rolls stay backup donors; unpriced rolls cannot start)",
                    slot_id,
                    start_block_kinds[slot_id],
                    min_start_g,
                )

        if match:
            used_tray_ids.add(match["global_tray_id"])
            comparisons.append({"slot_id": slot_id, "global_tray_id": match["global_tray_id"]})
            if trace:
                logger.info("[spool-select] slot=%s -> picked gtid=%s", slot_id, match["global_tray_id"])
        else:
            # THE total-outcome record: every unresolved requirement lands here,
            # whatever the cause, so no consumer has to re-derive "is this dispatchable"
            # by scanning the mapping array (which cannot tell a hole from a sparse
            # slot_id). Scoped to the same ``slot_id > 0`` the mapping build uses — a
            # malformed requirement carries no wire position, so it can neither be
            # dispatched against nor block a dispatch.
            if slot_id and slot_id > 0:
                unmatched_slots.append(slot_id)
            comparisons.append({"slot_id": slot_id, "global_tray_id": -1})
            if trace and slot_id not in start_block_kinds and slot_id not in pin_missing:
                logger.info("[spool-select] slot=%s -> NO MATCH", slot_id)

    outcome_extras = {
        "start_block_kinds": start_block_kinds,
        "pin_missing": pin_missing,
        "unmatched_slots": tuple(unmatched_slots),
    }
    if not comparisons:
        return MatchOutcome(mapping=None, **outcome_extras)

    max_slot_id = max(c["slot_id"] for c in comparisons)
    if max_slot_id <= 0:
        return MatchOutcome(mapping=None, **outcome_extras)

    mapping = [-1] * max_slot_id
    for c in comparisons:
        sid = c["slot_id"]
        if sid and sid > 0:
            mapping[sid - 1] = c["global_tray_id"]
    return MatchOutcome(mapping=mapping, **outcome_extras)


def _trace_rows(rows: list[dict], inv: dict[int, SlotInventory] | None) -> list[dict]:
    """Compact per-candidate view for the decision-trace INFO log."""
    out = []
    for f in rows:
        gtid = f.get("global_tray_id")
        si = inv.get(gtid) if inv else None
        out.append(
            {
                "gtid": gtid,
                "type": f.get("type"),
                "color": f.get("color"),
                "tii": f.get("tray_info_idx"),
                "remain": f.get("remain"),
                "inv_g": si.remaining_g if si else None,
                "first_ord": si.first_loaded_ord if si else None,
                "ord_src": si.ord_src if si else None,
                "unread": bool(f.get("unread")),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Slot-inventory construction: remaining grams + the first-loaded ordinal, in one query
# per mode. THE owner — the scheduler's remaining-grams-only delegate had no production
# caller and was deleted (2026-08-09).
# ---------------------------------------------------------------------------
def _dt_to_epoch(dt: datetime | None) -> float | None:
    """Epoch seconds for a datetime, treating a naive value as UTC.

    Every stamp source is UTC — ``utcnow()`` (``loaded_at`` / ``first_loaded_at``)
    and SQLite's ``created_at`` server-default — but SQLite returns them naive.
    ``datetime.timestamp()`` on a naive value assumes LOCAL time, shifting every
    ordinal by the host's UTC offset (harmless while uniform, but a real inversion
    hazard the moment any source or consumer mixes bases). Pinning naive → UTC makes
    the result an absolute epoch (surfaced via :func:`epoch_to_iso` / the trace)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _iso_to_epoch(value: str | None) -> float | None:
    """Epoch seconds for an ISO-8601 string (Spoolman ``first_used``)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return None


def epoch_to_iso(ord_: float | None) -> str | None:
    """UTC ISO-8601 rendering of a first-loaded ordinal for API responses."""
    if ord_ is None:
        return None
    try:
        return datetime.fromtimestamp(ord_, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


async def _is_spoolman_mode(db: AsyncSession) -> bool:
    """Single source: reuse the deficit service's mode check."""
    from backend.app.services.filament_deficit import _is_spoolman_mode as _mode

    return await _mode(db)


async def build_slot_inventory(db: AsyncSession, printer_id: int, loaded: list[dict]) -> dict[int, SlotInventory]:
    """Return ``{global_tray_id: SlotInventory}`` for AMS slots bound to an
    inventory spool (Bambuddy-side or Spoolman-side).

    Extends the legacy inventory-remain lookup with the first-loaded ordinal so
    the FIFO policy has a durable ordering signal. External / virtual-tray slots
    are skipped (tracked separately). Slots without a binding are absent from the
    map — the caller falls back to MQTT ``remain`` for those. Best-effort: an
    empty map on any failure.

    A loaded entry stamped ``unread`` (seated, no identity — see
    :attr:`SlotInventory.unread`) keeps its row so the flag reaches the trace and the
    ``/inventory-remain`` mirror, but its ``remaining_g`` is forced to ``None``: the
    binding names a roll the wire cannot confirm is the one in the tray, and quoting an
    unverifiable ledger figure is how a stale claim gets treated as available material.
    """
    if not loaded:
        return {}
    tracked_slots = [
        (f["ams_id"], f["tray_id"], f["global_tray_id"], bool(f.get("unread")))
        for f in loaded
        if not f.get("is_external")
    ]
    if not tracked_slots:
        return {}

    out: dict[int, SlotInventory] = {}

    if await _is_spoolman_mode(db):
        result = await db.execute(select(SpoolmanSlotAssignment).where(SpoolmanSlotAssignment.printer_id == printer_id))
        by_slot = {(a.ams_id, a.tray_id): a.spoolman_spool_id for a in result.scalars().all()}
        for ams_id, tray_id, gtid, unread in tracked_slots:
            spoolman_id = by_slot.get((ams_id, tray_id))
            if spoolman_id is None:
                continue
            remaining_g, first_ord = await _fetch_spoolman_slot(spoolman_id)
            if remaining_g is None and first_ord is None:
                continue
            out[gtid] = SlotInventory(
                remaining_g=None if unread else remaining_g,
                first_loaded_ord=first_ord,
                ord_src="first_used" if first_ord is not None else None,
                unread=unread,
            )
        return out

    # Internal inventory mode (default).
    result = await db.execute(
        select(SpoolAssignment)
        .options(selectinload(SpoolAssignment.spool))
        .where(SpoolAssignment.printer_id == printer_id)
    )
    by_slot = {(a.ams_id, a.tray_id): a.spool for a in result.scalars().all()}
    for ams_id, tray_id, gtid, unread in tracked_slots:
        spool = by_slot.get((ams_id, tray_id))
        if spool is None:
            continue
        # Seating-order precedence: the re-stampable loaded_at (a real re-seat /
        # binding change) beats the write-once first_loaded_at history, which beats
        # created_at. ord_src records which one won so the trace tells a genuine
        # reseat stamp from a stale-ledger fallback (006-H2S FIFO fix).
        if spool.loaded_at is not None:
            ord_src = "loaded_at"
        elif spool.first_loaded_at is not None:
            ord_src = "first_loaded_at"
        else:
            ord_src = "created_at"
        first_ord = _dt_to_epoch(spool.loaded_at or spool.first_loaded_at or spool.created_at)
        # Remaining grams have ONE origin: :attr:`Spool.remaining_g` (a spent row
        # derives 0.0 there — the gram ledger itself is never floored). The slot-level
        # ``unread`` verdict outranks it: that flag describes the tray's PHYSICAL
        # contents, which no row-level number can speak for.
        out[gtid] = SlotInventory(
            remaining_g=None if unread else spool.remaining_g,
            first_loaded_ord=first_ord,
            ord_src=ord_src,
            out_of_rotation=spool.feed_fault_at is not None,
            spent=spool.spent_at is not None,
            archived=spool.archived_at is not None,
            unread=unread,
        )
    return out


async def _fetch_spoolman_slot(spoolman_spool_id: int) -> tuple[float | None, float | None]:
    """One Spoolman fetch → (remaining_g, first_loaded_ord). Both ``None`` on any
    failure. Reads ``remaining_weight`` and ``first_used`` from the SAME dict."""
    try:
        from backend.app.services.spoolman import (
            SpoolmanClientError,
            SpoolmanNotFoundError,
            get_spoolman_client,
        )
    except ImportError:
        return None, None
    try:
        client = await get_spoolman_client()
        if client is None:
            return None, None
        spool = await client.get_spool(spoolman_spool_id)
    except (SpoolmanNotFoundError, SpoolmanClientError):
        return None, None
    except Exception as e:  # noqa: BLE001 — best-effort; a preference, not a guarantee
        logger.debug("Spoolman fetch failed for spool %s: %s", spoolman_spool_id, e)
        return None, None

    from backend.app.services.filament_deficit import _spoolman_grams_from_dict

    remaining_g = _spoolman_grams_from_dict(spool)
    first_ord = _iso_to_epoch(spool.get("first_used")) if isinstance(spool, dict) else None
    return remaining_g, first_ord


# ---------------------------------------------------------------------------
# Live dispatch outcome — THE one call every consumer of "what would this item
# dispatch with?" makes (scheduler, release path, manual-start route, run detail).
# ---------------------------------------------------------------------------
async def resolve_dispatch_outcome(
    db: AsyncSession, item: PrintQueueItem, printer_id: int | None = None
) -> MatchOutcome:
    """Run the matcher against LIVE tray state for ``item`` on a printer.

    Decision time IS execution time (2026-08-12 contract): nothing caches a mapping
    and replays it, so every consumer that needs to know what an item would dispatch
    with — the scheduler's gates, ``farm_staging``'s release re-check, the
    manual-start 409, the run-detail eligibility panel — asks HERE and gets the same
    answer the dispatch itself would produce. ``item.ams_mapping`` is read by the
    matcher as the operator's PIN input, never as a previous result.

    ``printer_id`` overrides the item's own pin for candidate evaluation (the
    model-based lane tries each idle printer in turn). Returns an empty outcome when
    no printer is resolvable — there is nothing to decide against.
    """
    pid = printer_id if printer_id is not None else item.printer_id
    if pid is None:
        return MatchOutcome(mapping=None)
    # Function-local import: print_scheduler imports this module at load time.
    from backend.app.services.print_scheduler import scheduler

    return await scheduler._compute_ams_mapping_for_printer(db, pid, item)


def mapping_json(outcome: MatchOutcome) -> str:
    """The outcome's mapping as the JSON string the storage/override params take.

    ``"[]"`` for a mapping-free outcome, which every reader already treats as "no
    mapping" — deliberately a VALUE rather than ``None``, because ``None`` means
    "fall back to the item's stored field" in the deficit API, and falling back would
    reintroduce reading a stored mapping as if it were a decision.
    """
    return json.dumps(outcome.mapping or [])


# ---------------------------------------------------------------------------
# Release-path guard
# ---------------------------------------------------------------------------
async def _read_min_start_g(db: AsyncSession) -> int:
    """Read the ``min_start_spool_g`` setting (default when unset/invalid)."""
    from backend.app.api.routes.settings import get_setting

    raw = await get_setting(db, "min_start_spool_g")
    if raw is None:
        return DEFAULT_MIN_START_SPOOL_G
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MIN_START_SPOOL_G


async def start_rule_block_kinds(db: AsyncSession, item: PrintQueueItem) -> dict[int, str]:
    """Recompute the pinned printer's selection outcome and return slot_id →
    ``START_BLOCK_*`` for every slot blocked purely by the minimum-start proof
    (below the floor, or unpriceable). Empty when the rule can't apply (no pinned
    printer, Print-Anyway acknowledged, or the floor is disabled).

    The floor's WORDING view of :func:`resolve_dispatch_outcome` — the manual-start
    409 needs to name which proof failed, and reading it off the same live outcome the
    dispatch decides on means the slot list and the reason can never come from two
    different computations. Callers asking the broader question ("is this item
    dispatchable at all?") read :attr:`MatchOutcome.is_total` on that outcome instead:
    a start-block is only one of the ways a requirement goes unresolved."""
    if item.printer_id is None or item.skip_filament_check:
        return {}
    if await _read_min_start_g(db) == 0:
        return {}
    outcome = await resolve_dispatch_outcome(db, item)
    return dict(outcome.start_block_kinds)
