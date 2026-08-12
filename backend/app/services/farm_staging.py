"""Low-spool staging release (Phase 4.2).

The dispatch scheduler's filament-deficit pre-flight (#1496) silently promotes
an item to ``manual_start=True`` + ``filament_short=True`` when the assigned
spool can't cover the print. That combination is the SYSTEM-staged marker
(operator staging is ``manual_start`` alone) — but nothing ever released it:
swapping the spool did NOT un-stage the item, so the queue looked stuck with no
recovery short of pressing "Print anyway" per row (P2-C).

Staging comes in two shapes and this path releases BOTH: pinned items (staged
on their assigned printer) and UNPINNED all-short items (``printer_id IS NULL``)
that the model-based candidate loop stages when every eligible printer is short.
A printer-scoped release therefore also re-checks unpinned items — a spool swap
on any one printer re-opens the fleet-wide candidate search on the next tick.

This module is the single release path. :func:`release_filament_staged`
re-runs the same ``compute_deficit_for_queue_item`` the scheduler used and
un-stages only the items whose deficit is actually gone — a still-short item
stays staged (no un-stage/re-stage bounce). It is invoked from three sites:

* ``main.on_ams_change`` via :func:`maybe_release_on_ams_change` — debounced by
  a per-printer tray-signature hash so the chatty AMS feed only triggers a
  release pass when a tray materially changed (spool swapped / refilled), and
  only when a staged farm item actually targets that printer;
* ``production_run.transition_run(resume)`` — an operator resume re-checks the
  run's printers before topping the run back up;
* ``POST /queue/release-staged`` — the queue page's explicit "Re-check and
  release" button.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import TYPE_CHECKING

from sqlalchemy import select

from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import spool_selection
from backend.app.services.filament_deficit import compute_deficit_for_queue_item

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# printer_id -> last-seen tray-signature hash. Module-level edge state, matching
# the fork's other event-edge bookkeeping (farm_stall, HMS dedup). Lost on
# restart — worst case is one extra release pass on the first AMS push.
_tray_signatures: dict[int, str] = {}

# D8 periodic-completeness safety net. The enumerated release triggers (AMS
# change / run resume / banner button) can ALL miss — the production incident:
# two staged items sat for hours while a fully-loaded printer went idle and no
# event fired. The scheduler tick calls :func:`maybe_release_periodic` to
# re-check fleet-wide, time-debounced by this monotonic stamp so the per-item
# 3MF deficit re-parse stays bounded even though the tick is faster. The
# tray-signature debounce above CANNOT serve here: it suppresses "nothing
# changed", which is exactly the case we must catch. Lost on restart — worst
# case one extra pass on the first post-restart tick.
_last_periodic_release: float | None = None
_PERIODIC_DEBOUNCE_S = 60.0

# Human-readable low-spool staging reason. The (manual_start + filament_short)
# FLAGS are the machine signal for staging identity — the queue banner and the
# release query below both key on them; ``waiting_reason`` is DISPLAY text. The
# un-staged waiting path already stores rich English sentences here ("Waiting
# for filament: 011-H2S (needs PETG) | Busy: ..."); staged items now do too,
# NAMING the blocked machine(s) so the operator walks to the RIGHT printer
# instead of a bare "filament_short" token (the D9 incident). Not token-shaped,
# so the frontend humanizer (waitingReason.ts) passes it through verbatim, and
# :func:`release_filament_staged` clears any reason carrying this prefix.
STAGING_REASON_PREFIX = "Low filament"


# What each start-block kind says to the operator. The two hold for different
# reasons and ask for different actions — top the roll up vs. tell the farm what
# the roll weighs — so a shared "below minimum" sentence would be a lie for half
# of them. Keys are ``spool_selection.START_BLOCK_*``; anything else (including
# None) is the generic grams-deficit hold.
_START_BLOCK_WHAT = {
    spool_selection.START_BLOCK_BELOW_FLOOR: "starting spool below minimum",
    spool_selection.START_BLOCK_UNKNOWN_GRAMS: "starting spool weight unknown",
}


def build_staged_reason(who: str, *, start_block: str | None = None) -> str:
    """Build the rich low-spool staging ``waiting_reason`` (see STAGING_REASON_PREFIX).

    ``who`` names the blocked machine(s): a pinned printer name, the short
    candidates of a model run, or a ``"<model> printers"`` fallback.
    ``start_block`` is the ``spool_selection.START_BLOCK_*`` kind when the hold is
    a minimum-start block (``spool_selection.dominant_start_block`` collapses a
    multi-slot / multi-printer hold to one kind); ``None`` is the generic
    "needs more filament" deficit hold.
    """
    who = (who or "").strip() or "assigned printer"
    what = _START_BLOCK_WHAT.get(start_block or "", "needs more filament")
    return f"{STAGING_REASON_PREFIX}: {who} ({what})"


def _reset_state() -> None:
    """Test hook: clear the module-level debounce state between cases."""
    global _last_periodic_release
    _tray_signatures.clear()
    _last_periodic_release = None


def compute_tray_signature(ams_data: list) -> str:
    """Stable hash of the spool-identity-bearing tray fields.

    Built from tray type / remaining % / RFID uuid per slot — the fields that
    change when a spool is swapped or refilled. Deliberately EXCLUDES volatile
    telemetry (humidity, temperatures) so routine AMS pushes hash identically
    and the release pass only runs on a material change.
    """
    parts: list[str] = []
    for ams in ams_data or []:
        if not isinstance(ams, dict):
            continue
        ams_id = ams.get("id")
        for tray in ams.get("tray", []) or []:
            if not isinstance(tray, dict):
                continue
            parts.append(
                f"{ams_id}:{tray.get('id')}:{tray.get('tray_type')}:{tray.get('remain')}:{tray.get('tray_uuid')}"
            )
    return hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()


async def _has_staged_farm_items(db: AsyncSession, printer_id: int) -> bool:
    """Cheap pre-check: does any SYSTEM-staged farm item target this printer?

    Farm = the item's batch has ``sku_file_id`` set. Keeps the AMS hook from
    paying the (3MF-parsing) deficit recompute on printers with nothing staged.
    Matches both pinned items (``printer_id == pid``) and UNPINNED all-short
    items (``printer_id IS NULL``) staged by the model-based candidate loop — a
    spool swap on ANY printer must re-open a fleet-wide redistribution.
    """
    result = await db.execute(
        select(PrintQueueItem.id)
        .join(PrintBatch, PrintQueueItem.batch_id == PrintBatch.id)
        .where(PrintBatch.sku_file_id.is_not(None))
        .where((PrintQueueItem.printer_id == printer_id) | (PrintQueueItem.printer_id.is_(None)))
        .where(PrintQueueItem.status == "pending")
        .where(PrintQueueItem.manual_start.is_(True))
        .where(PrintQueueItem.filament_short.is_(True))
        .limit(1)
    )
    return result.first() is not None


async def release_filament_staged(db: AsyncSession, printer_id: int | None = None) -> int:
    """Un-stage system-staged (low-spool) queue items whose deficit has cleared.

    Scans pending items with ``manual_start`` AND ``filament_short`` (optionally
    scoped to one printer), re-decides each one against live tray state
    (:func:`spool_selection.resolve_dispatch_outcome`) and re-runs
    :func:`compute_deficit_for_queue_item` against that decision, and for each item
    that is now fully dispatchable — empty deficit AND every requirement resolved:
    clears ``manual_start`` + ``filament_short`` (and the staging
    ``waiting_reason`` — any ``STAGING_REASON_PREFIX`` string or legacy token),
    so the next scheduler tick dispatches it. Items still short are left staged.
    Commits once; returns the
    number of items released. A per-item deficit-compute failure leaves that
    item staged (fail-safe) rather than releasing on unknown data.
    """
    query = (
        select(PrintQueueItem)
        .where(PrintQueueItem.status == "pending")
        .where(PrintQueueItem.manual_start.is_(True))
        .where(PrintQueueItem.filament_short.is_(True))
    )
    if printer_id is not None:
        # Include UNPINNED all-short items (printer_id NULL) staged by the
        # model-based candidate loop: they have no printer to scope by, and a
        # spool swap on ANY printer should re-open the fleet-wide search. Their
        # deficit recomputes to [] (no printer → filament_deficit returns []),
        # so they release and the next scheduler tick re-runs the candidate loop.
        query = query.where((PrintQueueItem.printer_id == printer_id) | (PrintQueueItem.printer_id.is_(None)))
    result = await db.execute(query)
    items = list(result.scalars().all())
    if not items:
        return 0

    released = 0
    for item in items:
        # Re-decide against LIVE state first: since ``ams_mapping`` became an operator
        # pin rather than a cached derivation, the item cannot answer "which trays
        # would this print feed from?" by itself — the mapping has to be computed, and
        # it is computed by the very function the scheduler dispatches on, so a hold
        # and its release can never disagree into a stage↔release bounce.
        try:
            outcome = await spool_selection.resolve_dispatch_outcome(db, item)
        except Exception as e:  # noqa: BLE001 — unknown state: keep it staged (fail-safe)
            logger.warning("farm_staging: dispatch re-check failed for item %s — left staged: %s", item.id, e)
            continue
        try:
            deficit = await compute_deficit_for_queue_item(
                db, item, ams_mapping_override=spool_selection.mapping_json(outcome)
            )
        except Exception as e:  # noqa: BLE001 — unknown spool state: keep it staged
            logger.warning("farm_staging: deficit re-check failed for item %s — left staged: %s", item.id, e)
            continue
        if deficit:
            continue  # still short — stays staged
        # An item can have an empty deficit yet still be undispatchable: the only
        # matching spool cannot be proven startable (a below-floor backup donor, or a
        # roll the ledger cannot price), no loaded roll matches a requirement at all,
        # or an operator pin names a tray that is not there. Releasing on grams alone
        # would let the scheduler re-stage it next tick forever, so the release owes
        # the WHOLE dispatch contract, not just the floor.
        if not outcome.is_total:
            continue
        item.manual_start = False
        item.filament_short = False
        # Clear the staging reason we authored — a rich "Low filament: ..." string
        # (STAGING_REASON_PREFIX) or a legacy bare token from a pre-D9 build — so a
        # released item never keeps a stale hold reason. An UNRELATED reason (never
        # set on a staged item today, but defensive) is left intact.
        # ``WAITING_REASON_UNREAD_PENDING`` is in the list even though the scheduler
        # never STAGES an item under it (that is the whole point of D2): a build that
        # staged an item on a phantom deficit and then held the same item for a read
        # could leave the token behind, and a released item must never keep a stale hold
        # reason. Cheap, and it keeps every filament-lane token clearing in ONE place.
        wr = item.waiting_reason
        if wr and (
            wr.startswith(STAGING_REASON_PREFIX)
            or wr
            in (
                "filament_short",
                spool_selection.WAITING_REASON_START_MIN,
                spool_selection.WAITING_REASON_UNREAD_PENDING,
                spool_selection.WAITING_REASON_PINNED_UNAVAILABLE,
            )
        ):
            item.waiting_reason = None
        released += 1
        logger.info(
            "farm_staging: released item %s on printer %s — filament deficit cleared",
            item.id,
            item.printer_id,
        )

    if released:
        await db.commit()
        # Wake the scheduler so freshly un-staged work dispatches immediately
        # (latency Phase A). Placed in the SERVICE — the single release path — so
        # every caller (route button, AMS-change hook, run resume, periodic net)
        # kicks exactly once without any of them double-firing. Guarded: a kick
        # failure must never turn a successful release into an error.
        try:
            from backend.app.services.dispatch_kick import dispatch_kick

            dispatch_kick.kick("release_staged", printer_id)
        except Exception:
            logger.debug("dispatch kick failed after staged release (non-fatal)", exc_info=True)
    return released


async def maybe_release_on_ams_change(printer_id: int, ams_data: list) -> int:
    """AMS-change hook: release staged items when this printer's trays changed.

    Debounced by :func:`compute_tray_signature` — the first push seeds the
    signature WITHOUT triggering a release (startup replay is not a spool
    swap); later pushes trigger only on a signature change, and only when a
    staged farm item targets the printer (cheap pre-check). Opens its own
    session (mirroring the eject monitor) so the AMS callback path never shares
    transaction state. Returns the released count; never raises.
    """
    try:
        sig = compute_tray_signature(ams_data)
        prev = _tray_signatures.get(printer_id)
        _tray_signatures[printer_id] = sig
        if prev is None or prev == sig:
            return 0

        from backend.app.core.database import async_session

        async with async_session() as db:
            if not await _has_staged_farm_items(db, printer_id):
                return 0
            return await release_filament_staged(db, printer_id)
    except Exception:  # noqa: BLE001 — must never crash the AMS callback chain
        logger.exception("farm_staging: AMS-change release pass failed for printer %s", printer_id)
        return 0


async def maybe_release_periodic(db: AsyncSession) -> int:
    """Scheduler-tick safety net (D8): fleet-wide staged-item release, debounced.

    Invoked from :meth:`PrintScheduler.check_queue` each tick. Runs the SAME
    single release path (:func:`release_filament_staged` fleet-wide) that the AMS
    hook, run-resume and banner button use, so staged work can't stall forever
    when none of those enumerated events fire (the incident: a fully-loaded
    printer went idle and nothing re-checked). Time-debounced by
    ``_PERIODIC_DEBOUNCE_S`` so the per-item 3MF deficit re-parse is bounded below
    the tick rate; steady state is free — :func:`release_filament_staged`
    short-circuits on an empty staged set before any 3MF is touched. Reuses the
    caller's session (the scheduler owns one). Never raises: a release failure
    must not kill the dispatch tick.
    """
    global _last_periodic_release
    try:
        now = time.monotonic()
        if _last_periodic_release is not None and (now - _last_periodic_release) < _PERIODIC_DEBOUNCE_S:
            return 0
        _last_periodic_release = now
        return await release_filament_staged(db, printer_id=None)
    except Exception:  # noqa: BLE001 — must never crash the scheduler tick
        logger.exception("farm_staging: periodic release pass failed")
        return 0
