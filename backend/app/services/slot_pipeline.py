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
  if it were a certainty is the false-merge hazard the 2026-08-01 audit named.
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
* **Serialization** — one asyncio lock per printer, so two pushes for one printer can
  never interleave their read-decide-write windows (this replaces ``main.py``'s
  ``_get_ams_assignment_lock`` at the W3b cutover; same pattern, ``main.py:540-548``).

**Never raises** (cross-cutting invariant 10): every entry point is fully guarded and a
single poisoned slot logs ERROR and is skipped — the rest of the pass still runs. A
farm-side failure may not break the MQTT callback chain.

Audit trail: there is no events table (operator ruling). Every APPLIED transition emits
the one structured line :func:`slot_state.format_slot_event` produces, through the
existing log pipeline / ``support/logs`` endpoint. KEEP / DEFER / NONE are DEBUG — they
happen on every push of every slot and would drown the lane.

Scope note (W3a): this module is NEW and UNWIRED. ``main.py``'s phase-1/phase-2 blocks,
``spool_tagless.handle_tagless_slot`` and the ``ams_presence`` lanes still own production
until W3b deletes them and points the callback here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from backend.app.core.websocket import ws_manager
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import ams_presence, spool_tagless
from backend.app.services.slot_state import (
    BindingView,
    Decision,
    DecisionKind,
    ResolutionContext,
    SlotState,
    SpoolView,
    derive_state,
    format_slot_event,
    resolve,
)
from backend.app.services.spool_binding import bind_spool_to_slot, release_spool_from_slot
from backend.app.services.spool_tag_matcher import create_spool_from_tray, find_spool_sharing_tray_uuid
from backend.app.services.tray_observation import TrayObservation
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

# Orchestrator-side release reason. Deliberately NOT in ``slot_state.RESOLUTION_REASONS``:
# an assignment whose spool row is gone never reaches the table at all (there is nothing
# to build a BindingView from), so the table can neither emit nor own this reason.
# Mirrors ``spool_tagless.py:1362``'s orphan drop, through the ONE unbind writer.
ORPHAN_RELEASE_REASON = "orphaned_assignment"

# Decision reason → the identify NEED verdict the discovery lane understands
# (``ams_presence.identify_needed``). The two ``*_owed_full_read`` defers only ever
# arise on a push that ASSERTED a tag, so the slot is tagged and a refresh is what is
# owed; ``identity_unresolved`` arises with no identity asserted at all, which is the
# discovery shape. Deriving the verdict from the reason avoids a second DB round-trip
# to re-answer a question the table already answered.
_IDENTIFY_VERDICT = {
    "identity_ambiguous_owed_full_read": "rfid_refresh",
    "partial_identity_owed_full_read": "rfid_refresh",
    "identity_unresolved": "discovery",
}

# How many last-location rows to inspect before giving up on a reclaim donor. The query
# is already ordered most-recent-first and filtered to ONE slot, so this is a runaway
# guard, not a policy knob.
_RECLAIM_SCAN_LIMIT = 25

# One lock per printer: a pass reads the slot's binding, decides, and writes, and two
# concurrent pushes interleaving those steps is how a slot gets two assignment rows
# (main.py:525-549 documents the IntegrityError this prevents). Process-lifetime, keyed
# by printer id — passes for DIFFERENT printers stay fully concurrent.
_pipeline_locks: dict[int, asyncio.Lock] = {}

# (slot, scanned tag) pairs whose sibling-tag KEEP has already been announced. A sibling
# read repeats on every push for as long as the roll faces that way, and the operator
# needs the fact once, not 3600 times an hour.
_sibling_logged: set[tuple[tuple[int, int, int], str]] = set()


def _reset_state() -> None:
    """Test hook: clear module-level locks + dedup state between cases."""
    _pipeline_locks.clear()
    _sibling_logged.clear()


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

    async def identify(self, printer_id: int, ams_id: int, tray_id: int, reason: str) -> None:
        """Ask the discovery lane for ONE read on this slot.

        Need-driven, never forced: ``command_identify`` re-checks the printer's state,
        the engaged extruder, drying and the client's own refusal, and spends nothing
        when any of them objects (doctrine rule 5 / invariant 4).
        """
        try:
            if self.schedule_identify is not None:
                await self.schedule_identify(printer_id, ams_id, tray_id, reason)
                return
            await ams_presence.command_identify(
                printer_id,
                ams_id,
                tray_id,
                source="reconcile",
                reason=_IDENTIFY_VERDICT.get(reason, "discovery"),
            )
        except Exception:  # noqa: BLE001 — an identify failure must not unwind the pass
            logger.exception(
                "[slot-state] identify scheduling failed for printer %d A%dT%d", printer_id, ams_id, tray_id
            )

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
    push (:func:`_believed_state`) and ``to_state`` is what this push establishes.
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
    try:
        async with _pipeline_lock(printer_id):
            seen: set[int] = set()
            for obs in observations:
                try:
                    transition = await _process_observation(obs, deps, seen)
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


async def _rollback(deps: PipelineDeps) -> None:
    try:
        await deps.db.rollback()
    except Exception:  # noqa: BLE001 — a failed rollback is already the worst case
        logger.exception("[slot-state] rollback failed")


# --- one slot ---------------------------------------------------------------


async def _process_observation(obs: TrayObservation, deps: PipelineDeps, seen: set[int]) -> AppliedTransition | None:
    printer_id, ams_id, tray_id = obs.slot

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

    decision, applied = await _apply(obs, deps, assignment, decision, seen)

    if applied:
        logger.info("%s", format_slot_event(printer_id, ams_id, tray_id, from_state, state, decision))
    return AppliedTransition(slot=obs.slot, from_state=from_state, to_state=state, decision=decision, applied=applied)


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


def _is_tagless(spool: Spool) -> bool:
    return not (spool.tag_uid or spool.tray_uuid)


def _binding_view(assignment: SpoolAssignment | None) -> BindingView | None:
    """The slot's current binding as the table sees it.

    The fingerprint comes from the ASSIGNMENT, never the spool: the assignment's
    snapshot is what "the filament this binding stands for" means (and what phase-1
    compared), while the spool's material/rgba is the roll's own identity and may have
    been edited by an operator.
    """
    if assignment is None or assignment.spool is None:
        return None
    spool = assignment.spool
    return BindingView(
        spool_id=spool.id,
        is_tagless=_is_tagless(spool),
        tag_uid=spool.tag_uid,
        tray_uuid=spool.tray_uuid,
        spent=spool.spent_at is not None,
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
        is_tagless=_is_tagless(spool),
        tag_uid=spool.tag_uid,
        tray_uuid=spool.tray_uuid,
        spent=spool.spent_at is not None,
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
    the uuid owner answers first; a bare tag falls back to strict equality. Deliberately
    NOT ``get_spool_by_tag``: its suffix/first-char variance lanes exist for legacy
    callers, and a widened row handed to the table as a certainty is the false-merge
    hazard (plan §"Root causes confirmed"). The table re-checks whatever arrives against
    the observation anyway, so a widened row could not be smuggled in — this just never
    produces one.
    """
    if obs.tray_uuid:
        spool = await find_spool_sharing_tray_uuid(db, obs.tray_uuid)
        if spool is not None:
            return spool
    if obs.tag_uid:
        res = await db.execute(
            select(Spool)
            .options(selectinload(Spool.k_profiles), selectinload(Spool.assignments))
            .where(func.upper(Spool.tag_uid) == obs.tag_uid.upper(), Spool.archived_at.is_(None))
            .limit(1)
        )
        return res.scalar_one_or_none()
    return None


def _fingerprint_compatible(obs: TrayObservation, spool: Spool) -> bool:
    """Same physical filament as this push, judged on the SPOOL's own identity.

    Same two one-origin helpers the table uses (``colors_similar`` +
    ``canonical_filament_type``), so "same filament" means one thing fork-wide.
    """
    if not colors_similar(obs.tray_color or "", spool.rgba or ""):
        return False
    return canonical_filament_type(obs.tray_type or "") == canonical_filament_type(spool.material or "")


async def _last_location_candidate(db: AsyncSession, obs: TrayObservation) -> Spool | None:
    """The reclaim donor: the roll most recently released FROM THIS SLOT that still
    fingerprint-matches what the wire now reports (doctrine rule 7).

    ``last_location_*`` is the durable residue ``spool_binding.release_spool_from_slot``
    stamps, which is what lets a pulled-and-returned roll keep its grams AND its FIFO
    position instead of minting a fresh 0 g row. Archived and spent rows are excluded:
    a retired roll may not reclaim a slot, and a spent one belongs to the W1 latch.
    """
    printer_id, ams_id, tray_id = obs.slot
    res = await db.execute(
        select(Spool)
        .options(selectinload(Spool.k_profiles), selectinload(Spool.assignments))
        .where(
            Spool.archived_at.is_(None),
            Spool.spent_at.is_(None),
            Spool.last_location_printer_id == printer_id,
            Spool.last_location_ams_id == ams_id,
            Spool.last_location_tray_id == tray_id,
        )
        .order_by(Spool.last_location_at.desc())
        .limit(_RECLAIM_SCAN_LIMIT)
    )
    for spool in res.scalars().all():
        if _fingerprint_compatible(obs, spool):
            return spool
    return None


# --- context ----------------------------------------------------------------


async def _build_context(obs: TrayObservation, deps: PipelineDeps, binding: BindingView | None) -> ResolutionContext:
    printer_id, ams_id, tray_id = obs.slot

    identity_candidate = None
    if obs.identity_asserted:
        candidate = await _identity_candidate(deps.db, obs)
        if candidate is not None:
            identity_candidate = _spool_view(candidate)

    last_location_candidate = None
    if binding is None and obs.config_nonempty and not obs.identity_asserted:
        donor = await _last_location_candidate(deps.db, obs)
        if donor is not None:
            last_location_candidate = _spool_view(donor)

    return ResolutionContext(
        binding=binding,
        identity_candidate=identity_candidate,
        last_location_candidate=last_location_candidate,
        qualified_cycle_pending=spool_tagless.qualified_cycle_pending(printer_id, ams_id, tray_id),
        auto_add_unknown=await _auto_add_unknown(deps),
        busy=_printer_busy(deps),
        settling=_settling(printer_id, ams_id, tray_id),
        identify_in_flight=_identify_in_flight(deps, ams_id),
        drying=_drying(deps, ams_id),
        tagless_default=await _tagless_default(deps),
    )


async def _auto_add_unknown(deps: PipelineDeps) -> bool:
    """``auto_add_unknown_rfid`` — unset means ON (mirrors ``main.py:2141-2142``)."""
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
) -> tuple[Decision, bool]:
    """Perform ``decision``. Returns the decision actually applied (a MINT may convert
    to a BIND) and whether the binding ledger changed."""
    kind = decision.kind

    if kind is DecisionKind.KEEP:
        await _apply_keep(obs, deps, assignment, decision)
        return decision, False

    if kind is DecisionKind.BIND:
        return await _apply_bind(obs, deps, decision, seen)

    if kind is DecisionKind.MINT:
        return await _apply_mint(obs, deps, decision, seen)

    if kind is DecisionKind.RECLAIM:
        return await _apply_reclaim(obs, deps, decision, seen)

    if kind is DecisionKind.RELEASE:
        return await _apply_release(obs, deps, assignment, decision)

    if kind is DecisionKind.REPLACE_SPENT:
        return await _apply_replace_spent(obs, deps, assignment, decision, seen)

    # DEFER / NONE: the two identity-owed shapes buy an answer instead of guessing;
    # everything else is a deliberate no-op this push.
    if decision.reason in _IDENTIFY_VERDICT:
        printer_id, ams_id, tray_id = obs.slot
        await deps.identify(printer_id, ams_id, tray_id, decision.reason)
    logger.debug(
        "[slot-state] printer=%d A%dT%d %s reason=%s",
        obs.printer_id,
        obs.ams_id,
        obs.tray_id,
        kind.value,
        decision.reason or "-",
    )
    return decision, False


async def _apply_keep(
    obs: TrayObservation, deps: PipelineDeps, assignment: SpoolAssignment | None, decision: Decision
) -> None:
    """KEEP: the binding is correct. Two side-effects only, neither a binding change."""
    if decision.reason == "sibling_tag_read":
        _log_sibling_read(obs, decision)
    if assignment is None or not obs.config_nonempty:
        return
    # Fingerprint refresh — mirrors main.py:1938-1953 / spool_tagless._refresh_assignment_
    # fingerprint: the snapshot tracks what the slot currently reports, so a
    # re-configured (but same) roll does not read as a different filament next push.
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


def _log_sibling_read(obs: TrayObservation, decision: Decision) -> None:
    """Announce a sibling-tag KEEP ONCE per (slot, scanned tag).

    This is the one KEEP where the stored identity visibly disagrees with the wire, so
    an operator reading the log must be able to see WHY it was still the same roll (the
    uuid matched). Repeating it every push would bury that.
    """
    scanned = (obs.tag_uid or "").upper()
    key = (obs.slot, scanned)
    if key in _sibling_logged:
        return
    _sibling_logged.add(key)
    logger.info(
        "[sibling-tag] printer=%d A%dT%d spool=%s read its second tag %s (stored %s)",
        obs.printer_id,
        obs.ams_id,
        obs.tray_id,
        decision.spool_id,
        scanned or "-",
        (obs.tray_uuid or "-"),
    )


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
    obs: TrayObservation, deps: PipelineDeps, decision: Decision, seen: set[int]
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
    return await _bind_spool(obs, deps, decision, spool, seen)


async def _bind_spool(
    obs: TrayObservation,
    deps: PipelineDeps,
    decision: Decision,
    spool: Spool,
    seen: set[int],
    *,
    fingerprint: tuple[str, str] | None = None,
) -> tuple[Decision, bool]:
    """The shared BIND write: the ONE binding writer + the pre-config one-shot."""
    printer_id, ams_id, tray_id = obs.slot
    pre_config = decision.reason == "pre_configured_apply"
    fp_color, fp_type = fingerprint if fingerprint is not None else (obs.tray_color or "", obs.tray_type or "")

    assignment = await bind_spool_to_slot(
        deps.db,
        spool,
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        fingerprint_color=fp_color,
        fingerprint_type=fp_type,
        origin=ORIGIN_PRECONFIG if pre_config else ORIGIN_BIND,
    )
    if assignment is None:
        # Damped move — the writer already logged the WARNING and wrote nothing, so
        # there is no binding change to announce.
        return decision, False
    if pre_config:
        # One-shot apply (main.py:2021-2054 semantics): the operator's intent is now a
        # real location claim, so the marker is cleared and the deferred configuration
        # finally goes out to the slot the firmware refused it on while empty.
        assignment.pre_configured_at = None
    await deps.db.commit()
    seen.add(spool.id)
    if pre_config:
        await deps.push_slot_config(spool, printer_id, ams_id, tray_id, _tray_dict(obs))
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
    )
    if assignment is None:
        return decision, False
    await deps.db.commit()
    seen.add(spool.id)
    logger.info(
        "[slot-state] printer=%d A%dT%d reclaimed spool %d from its last location (ordinal preserved)",
        printer_id,
        ams_id,
        tray_id,
        spool.id,
    )
    await _emit_auto_assigned(obs, deps, spool)
    return decision, True


async def _apply_release(
    obs: TrayObservation, deps: PipelineDeps, assignment: SpoolAssignment | None, decision: Decision
) -> tuple[Decision, bool]:
    if assignment is None:
        return decision, False
    await release_spool_from_slot(deps.db, assignment, reason=decision.reason)
    await deps.db.commit()
    await _emit_assignment_changed(obs, deps)
    return decision, True


async def _apply_mint(
    obs: TrayObservation, deps: PipelineDeps, decision: Decision, seen: set[int]
) -> tuple[Decision, bool]:
    """MINT, with the last-second existence recheck.

    The table decides to mint from what the caller resolved a moment ago. Between then
    and now a row may have appeared (a concurrent pass, an operator add) — and some
    mint rows exist precisely for contract-violating shapes. Re-asking the DB is cheap;
    a twin ledger row for one physical roll is not (the sibling-tag failure mode).
    """
    if obs.identity_asserted:
        owner = await _identity_candidate(deps.db, obs)
        if owner is not None:
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
            return await _bind_spool(obs, deps, converted, owner, seen)

    spool, from_default = await _mint_from_spec(deps, obs, decision.mint_spec or {})
    fingerprint = _mint_fingerprint(obs, decision.mint_spec or {}, from_default)
    _decision, applied = await _bind_minted(obs, deps, decision, spool, seen, fingerprint, from_default)
    return _decision, applied


async def _bind_minted(
    obs: TrayObservation,
    deps: PipelineDeps,
    decision: Decision,
    spool: Spool,
    seen: set[int],
    fingerprint: tuple[str, str],
    from_default: bool,
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
    if from_default:
        # The tray is bare or still carries the DEPARTED row's config, so the firmware
        # does not yet hold this row's identity — push it, exactly as the bare-tray
        # auto-config and ``_replace_row_after_cycle`` do.
        await deps.push_slot_config(spool, printer_id, ams_id, tray_id, _tray_dict(obs))
    await _emit_auto_assigned(obs, deps, spool)
    return decision, True


async def _apply_replace_spent(
    obs: TrayObservation,
    deps: PipelineDeps,
    assignment: SpoolAssignment | None,
    decision: Decision,
    seen: set[int],
) -> tuple[Decision, bool]:
    """The W1 silent spent→mint: the drained row retires, its replacement takes the slot.

    Mirrors ``spool_tagless._replace_row_after_cycle`` (spool_tagless.py:665-726) — the
    function W3b deletes — with ONE deliberate difference, stated here so the change is
    not mistaken for drift: the departed row is disposed through the fork's canonical
    disposal (``dispose_provisional_on_tag``), so a PRISTINE auto-minted row (no usage
    ledger) is hard-deleted instead of leaving an archived 0 g husk, while any
    ledger-bearing row is archived exactly as before. A row that is not ours to dispose
    (operator-created) is archived too: the roll ran out and physically left, so it must
    not keep claiming a slot.
    """
    printer_id, ams_id, tray_id = obs.slot
    if assignment is None or assignment.spool is None:
        return decision, False

    # Pre-gate: a dead roll re-seated without filament fed is not a swap — no churn
    # (mirrors ``spool_tagless``'s ``_tray_loaded`` gate in branch (3)).
    if not spool_tagless.tray_loaded(_tray_dict(obs)):
        logger.debug(
            "[slot-state] printer=%d A%dT%d spent-replace skipped: tray present but not loaded",
            printer_id,
            ams_id,
            tray_id,
        )
        return decision, False

    # Consume the qualified physical cycle — the ONE thing that releases the W1 latch,
    # and it is spent exactly here so a later push cannot replay the same swap.
    if not spool_tagless.consume_qualified_cycle(printer_id, ams_id, tray_id):
        logger.debug(
            "[slot-state] printer=%d A%dT%d spent-replace skipped: no qualified cycle to consume",
            printer_id,
            ams_id,
            tray_id,
        )
        return decision, False

    departed = assignment.spool
    disposition = await spool_tagless.dispose_provisional_on_tag(deps.db, departed)
    if disposition == "kept":
        departed.archived_at = datetime.utcnow()  # keep the ledger row + its grams
        disposition = "archived"
    await deps.db.flush()
    logger.info(
        "[slot-state] printer=%d A%dT%d spent spool %d %s on a qualified roll swap",
        printer_id,
        ams_id,
        tray_id,
        departed.id,
        disposition,
    )

    spool, from_default = await _mint_from_spec(deps, obs, decision.mint_spec or {})
    fingerprint = _mint_fingerprint(obs, decision.mint_spec or {}, from_default)
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


def _tray_dict(obs: TrayObservation) -> dict:
    """The observation as a tray dict, for the existing tray-shaped helpers.

    Only ASSERTED members are included — a key this push did not carry stays absent, so
    the helpers see exactly what the wire said (the atomic-pair rule survives the
    round-trip).
    """
    tray: dict = {"id": obs.tray_id}
    if obs.state is not None:
        tray["state"] = obs.state
    for key, value in (
        ("tag_uid", obs.tag_uid),
        ("tray_uuid", obs.tray_uuid),
        ("tray_type", obs.tray_type),
        ("tray_color", obs.tray_color),
        ("tray_info_idx", obs.tray_info_idx),
        ("tray_sub_brands", obs.tray_sub_brands),
        ("remain", obs.remain),
        ("nozzle_temp_min", obs.nozzle_temp_min),
        ("nozzle_temp_max", obs.nozzle_temp_max),
    ):
        if value is not None:
            tray[key] = value
    return tray


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
    """``spool_auto_assigned`` — the existing vocabulary (main.py:2390-2398).

    ``origin="tagless"`` is added for a row with no RFID identity, which is what the
    frontend toasts on (``useWebSocket.ts`` case ``spool_auto_assigned``); a tagged
    payload stays byte-identical to the RFID lane's.
    """
    printer_id, ams_id, tray_id = obs.slot
    payload: dict = {
        "type": "spool_auto_assigned",
        "printer_id": printer_id,
        "ams_id": ams_id,
        "tray_id": tray_id,
        "spool_id": spool.id,
    }
    if _is_tagless(spool):
        payload["origin"] = "tagless"
    await deps.emit(payload)


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
    "run_slot_pipeline",
]
