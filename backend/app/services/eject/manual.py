"""Manual "Eject plate" — the operator's one way to sweep a plate, whatever put it there.

The service answers with a VERDICT, never an exception. ``manual_eject`` returns exactly
one :class:`EjectVerdict` over the closed
:data:`~backend.app.schemas.printer.EjectOutcome`, and ``api/routes/printer_eject.py``
owns the single verdict → HTTP map (the 2026-08-20 ``slot_recheck`` precedent). Before
this rewrite the same lane raised three exception classes from four nesting levels, and
one of them signalled no failure at all — it was the confirm dialog the whole feature
exists to open, thrown as an error and reassembled by the caller from ``except`` ordering.

The five outcomes:

* ``dispatched`` / ``released_watch`` — the sweep is on its way. ``released_watch`` means
  an armed cooldown watch was signalled instead of a parallel dispatch being raced
  against it; the watch owns the single release path.
* ``needs_input`` — the eject is asking the operator for the two things only they have:
  the part height on the plate and the sweep profile. Not an error. Every refusal an
  operator's input can CURE arrives here — a plate the farm never dispatched, a farm unit
  with no eject profile, a gate naming a unit this lane may not sweep directly, and a
  donor that is only a container.
* ``bed_hot`` — a real live bed reading above the release threshold, with both numbers,
  so the confirm dialog is built on measurements rather than a missing value.
* ``refused`` — a state the operator's input cannot cure, carrying a closed
  :data:`~backend.app.schemas.printer.EjectRefusalReason`. The occupancy refusals keep the
  authority's own token spelling, so one vocabulary runs from the state machine to the
  dialog.

**Ordering is the design.** The operator declares occupancy FIRST, before the eject's own
precondition check: the 2026-08-30 cascade left printers gated behind sweeps that could
never run, and the cure — "there is a part on this plate, sweep it" — has to be reachable
while a unit is mid-upload. A declaration revokes that dispatch lease, so the unit unwinds
instead of printing onto the declared plate, and the brief ``dispatch_in_flight`` while the
scheduler unwinds is honest and self-clearing. The raise is NEVER rolled back — not on an
unresolvable donor, not on the hot-bed confirm, not on an abandoned dialog: the plate IS
occupied whatever happens next, and "Mark plate as cleared" is the visible undo.

Donor resolution lives in :mod:`backend.app.services.eject.donor` as a chain, and this
module composes rather than resolves: the operator lane walks
:data:`~backend.app.services.eject.donor.MANUAL_DONOR_CHAIN` (all three tiers, each one
below the first confirmed by a human looking at the plate) while the unattended lane below
walks :data:`~backend.app.services.eject.donor.AUTO_DONOR_CHAIN` (the strict tier alone).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.eject_profile import EjectProfile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.schemas.printer import EjectOrigin, EjectOutcome, EjectRefusalReason
from backend.app.services.eject import remote as eject_remote
from backend.app.services.eject.donor import (
    AUTO_DONOR_CHAIN,
    MANUAL_DONOR_CHAIN,
    DonorContext,
    deposit_donor,
    release_donor,
    resolve_donor,
)
from backend.app.services.eject.geometry import GeometryUnavailable, get_geometry_required
from backend.app.services.eject.monitor import _resolve_eject_threshold, eject_cooldown_monitor
from backend.app.services.plate_occupancy import (
    CooldownEject,
    Evidence,
    FirstArticleEject,
    TransitionRefusal,
    plate_occupancy,
)
from backend.app.services.plate_occupancy_store import latest_started_item
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.filename import print_identity_key

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EjectVerdict:
    """What one "Eject plate" call concluded. The service's answer, whole.

    Every field the route puts on the wire lives here, so the controller MAPS rather
    than decides. Fields are per-outcome and the constructors below are the only way to
    build one — a verdict cannot be assembled with the wrong fields set for its outcome.

    ``mode`` is DERIVED, not stored: the 200 body's ``mode`` is the outcome itself for
    the two success shapes, and a second stored spelling of one fact is how two lanes
    come to disagree about which one happened.
    """

    outcome: EjectOutcome
    # dispatched / released_watch
    queue_item_id: int | None = None
    # needs_input
    origin: EjectOrigin | None = None
    print_name: str | None = None
    max_z_height_mm: float | None = None
    suggested_eject_profile_id: int | None = None
    # bed_hot
    bed_c: float | None = None
    threshold_c: float | None = None
    # refused
    reason: EjectRefusalReason | None = None
    started: bool | None = None
    age_s: float | None = None

    @property
    def mode(self) -> str | None:
        """The 200 body's ``mode`` — the outcome itself, for the two success shapes."""
        return self.outcome if self.outcome in ("dispatched", "released_watch") else None

    @classmethod
    def dispatched(cls, queue_item_id: int | None) -> EjectVerdict:
        """The sweep was built, uploaded and started on the printer."""
        return cls(outcome="dispatched", queue_item_id=queue_item_id)

    @classmethod
    def released_watch(cls, queue_item_id: int) -> EjectVerdict:
        """An armed cooldown watch was signalled to release now — no parallel dispatch."""
        return cls(outcome="released_watch", queue_item_id=queue_item_id)

    @classmethod
    def needs_input(
        cls,
        *,
        origin: EjectOrigin,
        print_name: str | None,
        max_z_height_mm: float | None,
        suggested_eject_profile_id: int | None,
    ) -> EjectVerdict:
        """The eject needs the operator's part height and sweep profile to proceed."""
        return cls(
            outcome="needs_input",
            origin=origin,
            print_name=print_name,
            max_z_height_mm=max_z_height_mm,
            suggested_eject_profile_id=suggested_eject_profile_id,
        )

    @classmethod
    def bed_hot(cls, bed_c: float, threshold_c: float) -> EjectVerdict:
        """A REAL live bed reading above the release threshold; both numbers ride along."""
        return cls(outcome="bed_hot", bed_c=bed_c, threshold_c=threshold_c)

    @classmethod
    def refused(
        cls, reason: EjectRefusalReason, *, started: bool | None = None, age_s: float | None = None
    ) -> EjectVerdict:
        """A state the operator's input cannot cure.

        ``started`` / ``age_s`` ride ``eject_in_flight`` alone, because "an eject is
        already in flight" is two different situations to an operator: one the printer
        has STARTED (and will finish on its own) and one it has not yet acknowledged.
        """
        return cls(outcome="refused", reason=reason, started=started, age_s=age_s)


# The authority speaks in transition tokens; the eject lane speaks in refusal reasons.
# Every token is spelling-identical except ``not_occupied``, which reaches the API under
# the name it has always had there and now survives only for declare-less callers.
_REFUSAL_REASONS: dict[TransitionRefusal, EjectRefusalReason] = {
    "job_active": "job_active",
    "dispatch_in_flight": "dispatch_in_flight",
    "eject_in_flight": "eject_in_flight",
    "not_occupied": "no_plate_gate",
    "z_unreferenced": "z_unreferenced",
}


def _occupancy_refusal(printer_id: int, refusal: TransitionRefusal) -> EjectVerdict:
    """Turn an occupancy refusal into a verdict, enriching the one that needs facts."""
    reason = _REFUSAL_REASONS.get(refusal)
    if reason is None:
        # Not reachable from ``ejectable`` / ``declare_occupied``, whose refusal sets are
        # closed subsets of the map above. Fail loud rather than inventing a reason: a new
        # token in the authority must be answered here deliberately, not swallowed.
        raise AssertionError(f"eject lane has no refusal reason for occupancy token {refusal!r}")
    if reason != "eject_in_flight":
        return EjectVerdict.refused(reason)
    identity = plate_occupancy.eject_identity(printer_id)
    if identity is None:  # pragma: no cover — the refusal is derived from the same record
        return EjectVerdict.refused(reason)
    age_s = (
        (datetime.now(timezone.utc) - identity.dispatched_at).total_seconds()
        if identity.dispatched_at is not None
        else None
    )
    return EjectVerdict.refused(reason, started=identity.started_at is not None, age_s=age_s)


# --------------------------------------------------------------------------- #
# Which farm unit — if any — does this plate belong to?
# --------------------------------------------------------------------------- #
#: Where a plate's farm identity sends the eject. A closed lane rather than a pair of
#: booleans, so "an unapproved first article that is also directly sweepable" cannot be
#: expressed. ``first_article`` is derived from it (2026-08-30 review F18: FA-ness is an
#: EXPLICIT discriminator, never inferred from a refusal further down the flow).
ItemLane = Literal["eject", "needs_input", "first_article", "none"]


@dataclass(frozen=True)
class ItemResolution:
    """The farm unit this plate belongs to, and what may be done about it.

    ``item`` is populated for every lane but ``none`` — including ``needs_input``, where
    the unit cannot be swept directly but IS the donor anchor the operator's confirm
    builds from.
    """

    lane: ItemLane
    item: PrintQueueItem | None

    @property
    def first_article(self) -> bool:
        """The plate holds an unapproved first article; the approval flow owns it."""
        return self.lane == "first_article"


async def _resolve_manual_eject_item(db: AsyncSession, printer_id: int, plate_source: str | None) -> ItemResolution:
    """Resolve which farm unit ``printer_id``'s plate belongs to, and its lane.

    The AUTHORITY answers first: the plate's own policy is what the farm decided this
    deposit is for, and it is the same fact the armed watch was keyed on. A
    :class:`~backend.app.services.plate_occupancy.CooldownEject` names the unit this plate
    is cooling for; a
    :class:`~backend.app.services.plate_occupancy.FirstArticleEject` names one held for
    inspection, which must never be weakened into a blind sweep.

    Failing a policy, two DB questions — deliberately separate, because they answer
    different things:

    1. "Is the gate the printer's LATEST start, and is that unit sweepable?" The
       latest-start guard is what stops a foreign or screen-started print that finished
       after the farm unit from lending its identity to the wrong plate.
    2. "Does the gate name a farm unit AT ALL?" A farm-known plate is never treated as
       foreign — it becomes an operator confirm against that unit's own donor.
    """
    policy = plate_occupancy.snapshot(printer_id).plate_policy
    if isinstance(policy, FirstArticleEject):
        return ItemResolution(lane="first_article", item=await db.get(PrintQueueItem, policy.unit_id))
    if isinstance(policy, CooldownEject):
        item = await db.get(PrintQueueItem, policy.unit_id)
        if item is not None:
            # A first article is a first article whatever policy the plate carries. The
            # cooldown policy is never minted for one, so this can only fire on a
            # contradictory record — and the approval flow still wins, because the red
            # line is about the PART on the plate, not about how the farm labelled it.
            if item.first_article:
                return ItemResolution(lane="first_article", item=item)
            return ItemResolution(lane="eject", item=item)
        logger.warning(
            "manual_eject: printer %s plate is cooling for unit %s but that queue row is gone — "
            "falling back to the gate-matched ladder",
            printer_id,
            policy.unit_id,
        )

    if not plate_source:
        return ItemResolution(lane="none", item=None)

    latest = await latest_started_item(db, printer_id)
    if latest is not None and latest.dispatch_subtask_id == plate_source:
        if latest.first_article:
            return ItemResolution(lane="first_article", item=latest)
        if latest.status == "completed" and latest.eject_profile_id is not None:
            return ItemResolution(lane="eject", item=latest)
        return ItemResolution(lane="needs_input", item=latest)

    result = await db.execute(
        select(PrintQueueItem)
        .where(PrintQueueItem.dispatch_subtask_id == plate_source)
        .order_by(PrintQueueItem.id.desc())
        .limit(1)
    )
    gate_item = result.scalar_one_or_none()
    if gate_item is None:
        return ItemResolution(lane="none", item=None)
    return ItemResolution(lane="first_article" if gate_item.first_article else "needs_input", item=gate_item)


def _thermal_gate(state, threshold: float, *, allow_hot: bool) -> EjectVerdict | None:
    """The shared hot-bed precondition. ``None`` means the bed is cool enough to sweep.

    ``allow_hot`` skips it entirely. An unreadable live bed is a retryable
    ``bed_unreadable`` refusal (never a confirm dialog built on a missing reading — the
    UI would render ``Number(null)`` as "0 °C"); a real reading above ``threshold`` is a
    ``bed_hot`` verdict carrying live bed + threshold.
    """
    if allow_hot:
        return None
    bed = state.temperatures.get("bed") if state is not None and getattr(state, "connected", False) else None
    if bed is None:
        return EjectVerdict.refused("bed_unreadable")
    if bed > threshold:
        return EjectVerdict.bed_hot(bed, threshold)
    return None


async def _suggest_eject_profile_id(db: AsyncSession, printer_id: int) -> int | None:
    """The eject profile to pre-select in the confirm dialog: the most recently started
    eject-profiled unit on this printer (best guess of the operator's usual profile), or
    None when the printer has never run an eject-profiled unit."""
    result = await db.execute(
        select(PrintQueueItem.eject_profile_id)
        .where(
            PrintQueueItem.printer_id == printer_id,
            PrintQueueItem.eject_profile_id.is_not(None),
            PrintQueueItem.started_at.is_not(None),
        )
        .order_by(PrintQueueItem.started_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# --------------------------------------------------------------------------- #
# The one entry point
# --------------------------------------------------------------------------- #
async def manual_eject(
    db: AsyncSession,
    printer_id: int,
    *,
    allow_hot: bool = False,
    eject_profile_id: int | None = None,
    declare_occupied: bool = False,
    max_z_override: float | None = None,
) -> EjectVerdict:
    """Sweep the plate on ``printer_id``. Returns a verdict; raises only on infrastructure.

    Order (binding):

    1. the printer exists, and
    2. is connected — the two facts nothing downstream can work without;
    3. ``declare_occupied`` on a CLEAR plate raises the gate FIRST (revoking any dispatch
       lease), and only then is the eject's own occupancy gate consulted, so the
       operator's cure is reachable during an upload;
    4. the plate's farm identity resolves — an unapproved first article is refused here
       and nowhere else;
    5. a sweepable farm unit takes the direct path (armed watch signalled, else built and
       dispatched);
    6. anything else becomes an operator confirm: donor chain → prompt → dispatch.

    ``allow_hot`` is the explicit hot-bed override, ``eject_profile_id`` the operator's
    chosen sweep profile (its presence is what turns the confirm's prompt leg into its
    dispatch leg), and ``max_z_override`` their confirmed part height, which supersedes
    the donor's parsed header in the build — the donor may be an assumed identity or a
    bare container rather than the print actually on the plate.
    """
    printer = await db.get(Printer, printer_id)
    if printer is None:
        return EjectVerdict.refused("not_found")
    if not printer_manager.is_connected(printer_id):
        return EjectVerdict.refused("not_connected")

    state = printer_manager.get_status(printer_id)
    # ``z_reference`` comes from the eject lane's ONE origin so the manual dialog and
    # the automatic cooldown lane refuse a lost-Z printer on the same evidence — the
    # manual door is the one an operator reaches for after a reboot, so it is the one
    # that most needs the refusal to be reachable.
    ev = Evidence(
        live_state=getattr(state, "state", None),
        z_reference=eject_remote.z_reference_evidence(printer_id),
    )

    # (3) Declare-first. The confirm leg passes the flag again, but by then the gate is
    # up and this branch is skipped — a declaration can never double-raise, and it never
    # re-declares a standing gate (which would wipe the source id the confirm is
    # sweeping against).
    if declare_occupied and not plate_occupancy.is_plate_occupied(printer_id):
        refusal = plate_occupancy.declare_occupied(printer_id, ev)
        if refusal is not None:
            return _occupancy_refusal(printer_id, refusal)
        logger.info("manual_eject: printer %s plate declared occupied by operator (source-less gate)", printer_id)

    refusal = plate_occupancy.ejectable(printer_id, ev)
    if refusal is not None:
        return _occupancy_refusal(printer_id, refusal)

    # The ONE reader of the gate's source id. ``Printer.plate_gate_subtask_id`` is
    # write-only persistence since the 2026-08-30 cut-over: a failed persist leaves it
    # holding a dead key, and steering the donor off that key would sweep for a print
    # that is not on this plate.
    plate_source = plate_occupancy.plate_source(printer_id)

    resolution = await _resolve_manual_eject_item(db, printer_id, plate_source)
    if resolution.first_article:
        return EjectVerdict.refused("first_article")

    if resolution.lane == "eject" and resolution.item is not None:
        verdict = await _eject_farm_unit(db, printer_id, resolution.item, state=state, allow_hot=allow_hot)
        if verdict is not None:
            return verdict
        # The unit carries no eject profile after all (today's "Unit has no eject profile"
        # hard 409) — it is still the donor anchor, so it becomes an operator confirm.

    origin: EjectOrigin
    if resolution.item is not None:
        origin = "farm_unit"
    elif plate_source is None and declare_occupied:
        origin = "declared"
    else:
        origin = "foreign"
    return await _eject_by_confirm(
        db,
        printer,
        state,
        item=resolution.item,
        plate_source=plate_source,
        origin=origin,
        allow_hot=allow_hot,
        eject_profile_id=eject_profile_id,
        max_z_override=max_z_override,
    )


async def _eject_farm_unit(
    db: AsyncSession, printer_id: int, item: PrintQueueItem, *, state, allow_hot: bool
) -> EjectVerdict | None:
    """The direct path for a sweepable farm unit, or ``None`` when it has no profile.

    ``None`` is the ONE way out of this function that is not a verdict, and it means "this
    unit cannot be swept from its own record" — the caller turns that into an operator
    confirm built from the same unit's donor.

    An armed cooldown watch is DRIVEN, not raced: the plate's own
    :class:`~backend.app.services.plate_occupancy.CooldownEject` policy is the armed
    watch's identity, so signalling that watch's single release path is what keeps two
    dispatches off one printer.
    """
    threshold = await _resolve_eject_threshold(item.id)
    if threshold is None:
        return None

    hot = _thermal_gate(state, threshold, allow_hot=allow_hot)
    if hot is not None:
        return hot

    policy = plate_occupancy.snapshot(printer_id).plate_policy
    if isinstance(policy, CooldownEject) and policy.unit_id == item.id:
        if eject_cooldown_monitor.request_release_now(printer_id):
            logger.info(
                "manual_eject: signalled immediate release on printer %s (watch armed, item %s)", printer_id, item.id
            )
            return EjectVerdict.released_watch(item.id)

    # No armed watch (the DB-fallback path) → dispatch directly. EjectDispatchError
    # propagates: a build or transport failure is infrastructure, not a verdict.
    await eject_remote.dispatch_part_present_eject(
        db, printer_id=printer_id, queue_item_id=item.id, purpose="production", run_id=item.batch_id
    )
    logger.info("manual_eject: dispatched part-present eject on printer %s for item %s", printer_id, item.id)
    return EjectVerdict.dispatched(item.id)


async def _eject_by_confirm(
    db: AsyncSession,
    printer: Printer,
    state,
    *,
    item: PrintQueueItem | None,
    plate_source: str | None,
    origin: EjectOrigin,
    allow_hot: bool,
    eject_profile_id: int | None,
    max_z_override: float | None,
) -> EjectVerdict:
    """The operator confirm: donor chain → prompt → (profile + height) → dispatch.

    ONE lane for all three origins. A plate the farm never dispatched, a plate whose farm
    unit this lane may not sweep from its own record, and a plate the operator declared
    are the same problem — the farm cannot build a sweep it can vouch for on its own — and
    the same cure: show what the farm DOES know, let the operator correct the height and
    pick the profile, then sweep.

    The prompt leg DEPOSITS a re-fetched donor keyed by this gate so the confirm consumes
    it instead of downloading again; the confirm leg always releases it.
    """
    ctx = DonorContext(db=db, printer=printer, plate_source=plate_source, item=item)
    source = await resolve_donor(MANUAL_DONOR_CHAIN, ctx)
    if source is None:
        # Every tier declined: no gate archive, no last-item file on disk, and not one
        # library slice for this model. There is nothing to repack, so the operator's only
        # safe move is to clear the plate and lift the part off by hand.
        logger.info("manual_eject: printer %s has no resolvable eject donor (every tier declined)", printer.id)
        return EjectVerdict.refused("no_donor")

    # Prompt leg — no profile chosen yet.
    if eject_profile_id is None:
        deposit_donor(printer.id, plate_source, source.tmp_path)
        return EjectVerdict.needs_input(
            origin=origin,
            print_name=source.print_name,
            max_z_height_mm=source.max_z,
            suggested_eject_profile_id=await _suggest_eject_profile_id(db, printer.id),
        )

    profile = await db.get(EjectProfile, eject_profile_id)
    if profile is None:
        release_donor(source)
        return EjectVerdict.refused("profile_not_found")

    # A CONTAINER donor knows no height, and the sweep's clearance and lift are computed
    # from one. Asking again is the correct answer, not an error: the operator has the
    # part in front of them and the profile's own guard is still the ceiling.
    if source.max_z is None and max_z_override is None:
        deposit_donor(printer.id, plate_source, source.tmp_path)
        logger.info(
            "manual_eject: printer %s container-only donor confirmed without a part height — re-prompting",
            printer.id,
        )
        return EjectVerdict.needs_input(
            origin=origin,
            print_name=None,
            max_z_height_mm=None,
            suggested_eject_profile_id=eject_profile_id,
        )

    try:
        hot = _thermal_gate(state, profile.cooldown_temp_c, allow_hot=allow_hot)
        if hot is not None:
            return hot
        await eject_remote.dispatch_foreign_eject(
            db,
            printer_id=printer.id,
            profile_id=eject_profile_id,
            source_path=source.path,
            plate_id=source.plate_id,
            max_z_override=max_z_override,
        )
    finally:
        release_donor(source)

    logger.info(
        "manual_eject: dispatched operator-confirmed eject on printer %s (origin %s, plate %s, profile %s)",
        printer.id,
        origin,
        source.plate_id,
        eject_profile_id,
    )
    return EjectVerdict.dispatched(None)


# --------------------------------------------------------------------------- #
# Auto foreign-eject: a foreign completion that is positively the farm's OWN file
# (2026-07-18 decision) is auto-ejected after cooldown, no operator confirm.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ForeignFarmFile:
    """A foreign plate positively identified as the farm's OWN file — safe to
    auto-eject after cooldown. Carries the chosen eject ``profile_id``, the release
    ``threshold_c`` (the profile's ``cooldown_temp_c``) and the print name for logs."""

    profile_id: int
    threshold_c: float
    print_name: str | None


def _canonical_names(*names: str | None) -> set[str]:
    """The set of :func:`print_identity_key` forms of ``names``, blanks skipped.

    ONE key, not two: every call site below is an identity comparison
    (``isdisjoint`` / set intersection), and a stricter form adds nothing there —
    if two names key equal under the strict form they key equal under this one, so
    emitting both would only carry a redundant member that can never decide a
    comparison the relaxed member does not already decide.

    The key folds spaces→underscores, normalises the 3MF/gcode suffix AND drops the
    splicer's mid-stem ``.gcode`` token, so a screen-started print's UNDERSCORED USB
    echo (``…PCO-M12-2525_L1-90_spliced``) compares equal to the farm's stored
    ``…PCO-M12-2525.gcode_L1-90_spliced.3mf``. Without that token removal this gate
    refused every production plate — the whole corpus carries it (2026-08-22)."""
    out: set[str] = set()
    for name in names:
        if not name:
            continue
        try:
            key = print_identity_key(name)
        except TypeError:  # non-str duck type — skip rather than crash the identity check
            continue
        if key:
            out.add(key)
    return out


async def _farm_dispatched_names(db: AsyncSession, printer_id: int) -> set[str]:
    """Canonical names of every FARM file dispatched to ``printer_id``: the library
    filenames and archive filenames of the printer's farm queue items (a batch with a
    ``sku_file_id``, or an item carrying an ``eject_profile_id``). The identity corpus
    a foreign completion's echoed name is checked against before auto-ejecting it."""
    from backend.app.models.library import LibraryFile
    from backend.app.models.print_batch import PrintBatch

    result = await db.execute(
        select(PrintQueueItem)
        .outerjoin(PrintBatch, PrintQueueItem.batch_id == PrintBatch.id)
        .where(PrintQueueItem.printer_id == printer_id)
        .where((PrintQueueItem.eject_profile_id.is_not(None)) | (PrintBatch.sku_file_id.is_not(None)))
    )
    names: set[str] = set()
    for item in result.scalars().all():
        if item.archive_id is not None:
            archive = await db.get(PrintArchive, item.archive_id)
            if archive is not None:
                names |= _canonical_names(archive.filename)
        if item.library_file_id is not None:
            library_file = await db.get(LibraryFile, item.library_file_id)
            if library_file is not None:
                names |= _canonical_names(library_file.filename)
    return names


async def identify_farm_file_foreign(
    db: AsyncSession, printer_id: int, *, subtask_name: str | None, filename: str | None
) -> ForeignFarmFile | None:
    """Decide whether a FOREIGN completion is positively the farm's OWN file, so the
    farm may auto-eject it after cooldown instead of only escalating (2026-07-18).

    Returns a :class:`ForeignFarmFile` (profile + release threshold) ONLY when ALL of:

      (a) the echoed ``subtask_name``/``filename`` matches a file the farm has
          dispatched to THIS printer, both sides keyed through
          :func:`print_identity_key` (a screen-started print echoes the UNDERSCORED
          USB name with the splicer's mid-stem ``.gcode`` token dropped; the farm
          library stores the SPACED, token-bearing display name — only that key
          makes them compare equal);
      (b) the printer model's geometry row is hardware-``validated`` (production eject
          never runs on an unvalidated model);
      (c) a suggested eject profile exists for this printer;
      (d) the STRICT donor tier resolves and its parsed max Z height is within that
          profile's ``max_part_height_mm`` guard.

    Gate (d) walks :data:`~backend.app.services.eject.donor.AUTO_DONOR_CHAIN` — the
    gate-archive tier alone. The operator lane's assumed and container tiers are
    deliberately unreachable here: this decides to sweep a plate with nobody watching.

    Any miss → None (the caller falls back to the escalation-only hold). The cheap
    checks run BEFORE the donor resolution (which may FTPS re-fetch) so the common
    negative — a genuinely foreign print — exits fast without touching the wire. The
    helper opens no session of its own (the caller owns ``db``, per convention) and
    cleans up any temp re-fetch it makes.

    EVERY refusal logs its own gate and the values it refused on. The caller
    (``main._foreign_auto_eject``) only logs on an EXCEPTION, so a clean ``None``
    used to leave no trace anywhere — which is exactly how gate (a) refusing the
    entire production corpus went unnoticed for weeks (2026-08-22). Grep
    ``identify_farm_file_foreign: printer`` to see which gate is refusing."""
    # (a) name match against farm-dispatched files on this printer — the strongest,
    # cheapest signal, so it gates everything else.
    echoed = _canonical_names(subtask_name, filename)
    if not echoed:
        logger.info(
            "identify_farm_file_foreign: printer %s NOT identified — gate (a) no echoed name "
            "(subtask_name=%r, filename=%r)",
            printer_id,
            subtask_name,
            filename,
        )
        return None
    dispatched = await _farm_dispatched_names(db, printer_id)
    if echoed.isdisjoint(dispatched):
        logger.info(
            "identify_farm_file_foreign: printer %s NOT identified — gate (a) name mismatch; "
            "echoed=%s vs %d farm-dispatched name(s)=%s",
            printer_id,
            sorted(echoed),
            len(dispatched),
            sorted(dispatched),
        )
        return None

    printer = await db.get(Printer, printer_id)
    if printer is None:
        logger.info(
            "identify_farm_file_foreign: printer %s NOT identified — gate (a) printer row missing",
            printer_id,
        )
        return None

    # (b) model geometry must be hardware-validated (fail-closed, never auto-eject an
    # unvalidated model's envelope).
    try:
        await get_geometry_required(db, printer.model, require_validated=True)
    except GeometryUnavailable as exc:
        logger.info(
            "identify_farm_file_foreign: printer %s NOT identified — gate (b) geometry unusable (model=%r): %s",
            printer_id,
            printer.model,
            exc,
        )
        return None

    # (c) a profile to sweep with — the printer's usual eject profile.
    profile_id = await _suggest_eject_profile_id(db, printer_id)
    if profile_id is None:
        logger.info(
            "identify_farm_file_foreign: printer %s NOT identified — gate (c) no eject profile to "
            "suggest (no prior eject-profiled unit on this printer)",
            printer_id,
        )
        return None
    profile = await db.get(EjectProfile, profile_id)
    if profile is None:
        logger.info(
            "identify_farm_file_foreign: printer %s NOT identified — gate (c) suggested eject profile %s row missing",
            printer_id,
            profile_id,
        )
        return None

    # (d) the strict donor resolves + part height within the profile's guard. Clean up
    # any temp re-fetch either way (the auto-eject dispatch re-resolves the donor fresh at
    # release time, exactly like the manual confirm).
    plate_source = plate_occupancy.plate_source(printer_id)
    source = await resolve_donor(
        AUTO_DONOR_CHAIN, DonorContext(db=db, printer=printer, plate_source=plate_source, item=None)
    )
    if source is None:
        logger.info(
            "identify_farm_file_foreign: printer %s NOT identified — gate (d) strict donor unresolvable", printer_id
        )
        return None
    try:
        if source.max_z is None or source.max_z > profile.max_part_height_mm:
            logger.info(
                "identify_farm_file_foreign: printer %s NOT identified — gate (d) part height %s "
                "exceeds profile %s guard %.1fmm",
                printer_id,
                source.max_z,
                profile_id,
                profile.max_part_height_mm,
            )
            return None
    finally:
        # DEPOSIT the re-fetched donor so the LATER auto-eject dispatch
        # (``dispatch_identified_foreign_eject``, after the cooldown watch) consumes it
        # instead of downloading again. Keyed by the gate; expires with the TTL if the
        # cooldown outlives it (dispatch then re-fetches — fail-open). An on-disk donor
        # deposits nothing.
        deposit_donor(printer_id, plate_source, source.tmp_path)

    logger.info(
        "identify_farm_file_foreign: printer %s foreign plate IS the farm's own file "
        "(profile %s, cooldown %.1f°C, max_z %.1fmm) — auto-eject eligible",
        printer_id,
        profile_id,
        profile.cooldown_temp_c,
        source.max_z,
    )
    return ForeignFarmFile(profile_id=profile_id, threshold_c=profile.cooldown_temp_c, print_name=source.print_name)
