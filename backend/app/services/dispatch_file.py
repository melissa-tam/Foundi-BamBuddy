"""THE owner of "which bytes does this dispatch upload".

One print-upload lane exists (``print_scheduler._start_print``), and before this module
it decided that question inline: a settings read, a JSON parse, a marker-anchored
injection and an "if it produced a file, use it" fallback, all spelled out between the
archive copy and the FTPS upload. A second per-dispatch transform — the chute-prime
rewrite — would have made that two rewrite sites, two repack passes over a
hundreds-of-MB container and two places to reason about which bytes the printer
actually gets. So the question moved HERE, and the scheduler asks it once.

:func:`build_dispatch_file` builds an ordered **transform stack** — each step a
fingerprint plus a pure ``bytes -> bytes`` over the plate G-code member — folds it into
ONE repack, and returns a verdict:

* ``derived`` with a caller-owned temp to upload,
* ``unmodified`` with ``path=None``, meaning upload the durable source itself,
* ``build_failed`` with ``path=None``, same instruction, different reason.

Today's three steps, in this order:

1. **chute prime** (:mod:`backend.app.services.chute_prime`) — relocates the slicer's
   start-block prime bead off the plate's front lip and into the purge chute. FIRST,
   because it swaps a byte PREFIX of the member and therefore must see the file's own
   head, not one a later step has already edited.
2. **plate blow-off** (:mod:`backend.app.services.plate_blowoff`) — inserts a full-speed
   auxiliary-fan pulse immediately before the start block's bed-leveling banner, so the
   fan clears stray filament off the plate. It edits the member it is HANDED — chute
   prime's splice already in it — and never a head of its own read from the source, so
   the two steps cannot disagree about which bytes they saw. Its insert sits ABOVE the
   nozzle-load section chute prime rewrites, so neither step moves the other's anchor.
3. **snippets** — the upstream per-model start/end G-code injection (#422), per-job via
   ``PrintQueueItem.gcode_injection`` and per-model via the ``gcode_snippets`` setting.

**The cache key is DERIVED from the stack**, never assembled beside it. That is the
rollback lever, not a tidiness point: turning ``farm_chute_prime_enabled`` (or
``farm_plate_blowoff_enabled``) off removes the step, which removes its fingerprint,
which changes the key — so the very next dispatch of a file that was rewritten an hour
ago CANNOT be served the rewritten artifact out of the cache. A key assembled separately
from the steps would let the switch move the code path and not the bytes, which is the
worst possible rollback. The blow-off's fingerprint carries its pulse length as well, so
a new ``farm_plate_blowoff_seconds`` re-derives rather than replaying the old dwell.

**There is deliberately no refusal memo.** The chute prime's refusal is decided from the
bounded start block alone (``threemf_tools.read_plate_gcode_start_block`` — tens of KB,
not the hundreds-of-MB member), so re-deciding it per dispatch costs one small read. The
blow-off decides inside the build, on the member it is handed, so its verdict is carried
by the cached artifact itself. A memo would be a second cache with its own invalidation
rules beside one that already works.

**The durable and archive copies stay the ORIGINAL bytes.** Only the upload is derived.
Eject donors, reprints, retries and the operator's own re-slices therefore all start
from what was uploaded to the library, and a recipe change reaches every one of them by
re-deriving rather than by a migration nobody would run.

Operator ruling (2026-09-19): a file whose start block this fleet's recipe does not
recognise DISPATCHES UNMODIFIED with a warning. It never holds the queue and never
raises — hence the one boundary ``except`` at the bottom of this module: a bug in a
transform costs the farm its start-block edits, not a print.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.schemas.settings import AppSettings
from backend.app.services import chute_prime, derived_3mf_cache, plate_blowoff
from backend.app.utils.threemf_tools import (
    apply_gcode_snippets,
    read_plate_gcode_start_block,
    transform_plate_gcode,
)

logger = logging.getLogger(__name__)

#: The dispatch lane's own cache namespace (a sibling of the eject lane's ``eject_cache``
#: under the same data root).
_NAMESPACE = "dispatch_cache"

#: 1 GiB. Dispatch artifacts are FULL print containers — meshes and thumbnails included,
#: since the printer shows them — unlike the slim motion-only eject build. The production
#: corpus's largest is ~20 MB, so a gigabyte keeps a busy SKU mix resident across a shift
#: instead of re-deflating the same archive for every unit of a run.
_MAX_BYTES = 1024 * 1024 * 1024

#: The greppable prefix every chute-prime verdict is logged under, so one search over the
#: production log answers "what did the rewrite do to today's dispatches".
_LOG = "[chute-prime]"

#: The same, for the plate blow-off's verdicts.
_BLOWOFF_LOG = "[plate-blowoff]"

#: The seam's own prefix, for what no single step owns: the stack's build failing, or the
#: seam itself raising. Each step's verdicts stay under that step's prefix.
_SEAM_LOG = "[dispatch-file]"

#: The pulse length validated EXACTLY as the settings API validates it — through the
#: schema's own field (type, ``ge``, ``le``) — so its range has one declared origin.
_BLOWOFF_SECONDS: TypeAdapter[int] = TypeAdapter(Annotated[int, AppSettings.model_fields["farm_plate_blowoff_seconds"]])


@dataclass(frozen=True)
class DispatchFile:
    """Which bytes this dispatch uploads.

    ``path`` is a caller-owned temp file to upload and then unlink (it lands in the
    system temp dir, so ``bambu_ftp.cleanup_downloaded_3mf`` accepts it); ``None`` means
    upload the durable source unchanged. ``outcome`` says WHY — ``unmodified`` is the
    ordinary "nothing to apply", ``build_failed`` is "we wanted to and could not", and
    keeping them apart is what lets a dispatch that silently lost its chute prime be
    told from one that never had a step to begin with.
    """

    path: Path | None
    outcome: Literal["derived", "unmodified", "build_failed"]


@dataclass(frozen=True)
class _Step:
    """One transform in the stack: what it is, and what it does.

    ``fingerprint`` identifies the transform AND its inputs (a recipe version, a pulse
    length, a hash of the resolved snippets) — it is the step's whole contribution to the
    cache key, so a step whose behaviour changes without its fingerprint changing serves
    stale bytes. ``apply`` is a deterministic ``bytes -> bytes`` over the plate G-code
    member — the same member always gives the same bytes; it may log its own verdict, and
    it may raise: the cache turns a raising builder into ``BuildFailed`` with a traceback.
    """

    fingerprint: str
    apply: Callable[[bytes], bytes]


async def _switch_on(db: AsyncSession, key: str, log: str) -> bool:
    """A step's kill switch, read HERE and nowhere else.

    Schema default first, DB override on top, schema default again on a read failure —
    the house idiom (``eject/monitor._resolve_stall_settings``). A settings-store
    failure must not decide the feature: the recipe either applies or it does not, and
    "the DB blinked" is not an argument for either.
    """
    from backend.app.api.routes.settings import get_setting

    enabled = bool(AppSettings.model_fields[key].default)
    try:
        raw = await get_setting(db, key)
        if raw is not None:
            enabled = raw.strip().lower() == "true"
    except Exception:  # noqa: BLE001 — a settings read failure falls back to the schema
        logger.exception("%s settings read failed — using the schema default (%s)", log, enabled)
    return enabled


async def _plate_blowoff_seconds(db: AsyncSession) -> int:
    """The blow-off's pulse length, read HERE and nowhere else.

    The switch's contract, plus one arm: a stored value the schema would refuse is the
    schema default and a WARNING. The PUT route validates against the same field, so such
    a value only arrives by editing the settings table by hand — and a hand-typed dwell
    outside the range the operator can choose must not hold every print's start.
    """
    from backend.app.api.routes.settings import get_setting

    seconds = int(AppSettings.model_fields["farm_plate_blowoff_seconds"].default)
    try:
        raw = await get_setting(db, "farm_plate_blowoff_seconds")
    except Exception:  # noqa: BLE001 — a settings read failure falls back to the schema
        logger.exception("%s settings read failed — using the schema default (%s s)", _BLOWOFF_LOG, seconds)
        return seconds
    if raw is None:
        return seconds
    try:
        return _BLOWOFF_SECONDS.validate_python(raw.strip())
    except ValidationError:
        logger.warning(
            "%s stored farm_plate_blowoff_seconds %r is outside the schema — using the default (%s s)",
            _BLOWOFF_LOG,
            raw,
            seconds,
        )
        return seconds


async def _chute_prime_step(db: AsyncSession, source_path: Path, plate_id: int, item_id: int) -> _Step | None:
    """The chute-prime step, or None with the reason logged.

    Every outcome is decidable from the machine-start head, so the read is bounded and
    runs off the loop. The head is captured in the closure: ``apply`` splices the
    rewritten head back over the member's prefix, which is only valid for the exact
    bytes the rewrite saw — hence the ``startswith`` assertion.
    """
    if not await _switch_on(db, "farm_chute_prime_enabled", _LOG):
        return None

    head = await asyncio.to_thread(read_plate_gcode_start_block, source_path, plate_id)
    if head is None:
        # Indistinguishable from a refusal to the operator, and it means the same thing:
        # this file is not one the recipe can read, so it ships as sliced.
        logger.warning(
            "%s item %s: no machine-start block readable in plate %s of %s — dispatching unmodified",
            _LOG,
            item_id,
            plate_id,
            source_path.name,
        )
        return None

    outcome = chute_prime.rewrite_head(head)
    if isinstance(outcome, chute_prime.Refused):
        logger.warning(
            "%s item %s: start block not recognised in plate %s of %s — dispatching unmodified (%s: %s)",
            _LOG,
            item_id,
            plate_id,
            source_path.name,
            outcome.reason,
            outcome.detail,
        )
        return None
    if isinstance(outcome, chute_prime.AlreadyRewritten):
        # Reachable in production: a screen-restarted job is archived from the
        # printer-resident — already rewritten — copy, which can return as a donor.
        logger.debug("%s item %s: plate %s of %s is already chute-primed", _LOG, item_id, plate_id, source_path.name)
        return None

    logger.info(
        "%s item %s: prime relocated to chute (E=%.1f mm) plate %s of %s",
        _LOG,
        item_id,
        outcome.prime_mm,
        plate_id,
        source_path.name,
    )

    rewritten_head = outcome.head

    def _apply(member: bytes) -> bytes:
        if not member.startswith(head):
            raise ValueError(
                f"plate {plate_id} of {source_path.name} no longer starts with the head the rewrite read "
                "(the donor changed under the dispatch)"
            )
        return rewritten_head + member[len(head) :]

    return _Step(chute_prime.RECIPE_VERSION, _apply)


async def _plate_blowoff_step(db: AsyncSession, source_path: Path, plate_id: int, item_id: int) -> _Step | None:
    """The plate blow-off step, or None when its switch is off.

    Unlike the chute prime, nothing is decided up front: ``apply`` rewrites the member
    the stack HANDS it, so the verdict is reached — and logged — inside the BUILD. That
    is once per derived artifact, not once per dispatch: a cache hit replays bytes whose
    verdict was logged when they were built. A refusal hands back the member unchanged,
    so the file ships with exactly what the earlier steps made of it.

    The fingerprint carries the recipe AND the pulse length, because both change the
    bytes.
    """
    if not await _switch_on(db, "farm_plate_blowoff_enabled", _BLOWOFF_LOG):
        return None
    seconds = await _plate_blowoff_seconds(db)

    def _apply(member: bytes) -> bytes:
        outcome = plate_blowoff.insert_blowoff(member, seconds=seconds)
        if isinstance(outcome, plate_blowoff.Refused):
            logger.warning(
                "%s item %s: no blow-off for plate %s of %s — dispatching without it (%s: %s)",
                _BLOWOFF_LOG,
                item_id,
                plate_id,
                source_path.name,
                outcome.reason,
                outcome.detail,
            )
            return member
        if isinstance(outcome, plate_blowoff.AlreadyRewritten):
            # Reachable in production for the same reason as the chute prime's: a
            # screen-restarted job is archived from the printer-resident copy.
            logger.debug(
                "%s item %s: plate %s of %s already carries a blow-off",
                _BLOWOFF_LOG,
                item_id,
                plate_id,
                source_path.name,
            )
            return member
        logger.info(
            "%s item %s: %s s aux-fan blow-off inserted before bed leveling (line %s) in plate %s of %s",
            _BLOWOFF_LOG,
            item_id,
            seconds,
            outcome.anchor_line,
            plate_id,
            source_path.name,
        )
        return outcome.text.encode("utf-8")

    return _Step(f"{plate_blowoff.RECIPE_VERSION}:{seconds}s", _apply)


async def _snippet_step(db: AsyncSession, item_id: int, printer_model: str | None) -> _Step | None:
    """The upstream per-model start/end snippet step, or None.

    The ``gcode_snippets`` setting is a JSON object keyed by printer model; its read and
    parse live HERE now (they were inline in the scheduler) with the same tolerance they
    had there — a missing, unparseable or model-less blob is a WARNING and no step, never
    a failed dispatch.
    """
    from backend.app.api.routes.settings import get_setting

    try:
        raw = await get_setting(db, "gcode_snippets")
        if not raw:
            return None
        model_snippets = json.loads(raw).get(printer_model, {})
        start_gc = (model_snippets.get("start_gcode") or "").strip() or None
        end_gc = (model_snippets.get("end_gcode") or "").strip() or None
    except Exception as exc:  # noqa: BLE001 — a bad snippet blob dispatches the original
        logger.warning("Queue item %s: G-code snippet load failed, using original: %s", item_id, exc)
        return None

    if not start_gc and not end_gc:
        return None

    logger.info("Queue item %s: G-code injected for model %s", item_id, printer_model)

    digest = hashlib.sha256(f"{start_gc or ''}\0{end_gc or ''}".encode()).hexdigest()

    def _apply(member: bytes) -> bytes:
        # utf-8 with errors="ignore" is what this injection has always decoded with: a
        # sliced member is ASCII G-code plus whatever the operator typed into an object
        # name, and a stray byte there must not cost the dispatch its snippets.
        text = member.decode("utf-8", errors="ignore")
        return apply_gcode_snippets(text, start_gc, end_gc).encode("utf-8")

    return _Step(f"snippets:{digest}", _apply)


def _fold(steps: tuple[_Step, ...]) -> Callable[[bytes], bytes]:
    """The stack as ONE ``bytes -> bytes``, applied in order."""

    def _transform(member: bytes) -> bytes:
        for step in steps:
            member = step.apply(member)
        return member

    return _transform


async def build_dispatch_file(
    db: AsyncSession,
    source_path: Path,
    plate_id: int,
    *,
    item_id: int,
    gcode_injection: bool,
    printer_model: str | None,
) -> DispatchFile:
    """Decide — and build — the bytes this dispatch uploads.

    ``source_path`` is the DURABLE copy (library file or archive copy) and is never
    modified. ``gcode_injection`` is the queue item's own per-job snippet toggle;
    ``printer_model`` selects the snippet set. Returns :class:`DispatchFile`; never
    raises.
    """
    try:
        steps: list[_Step] = []
        prime = await _chute_prime_step(db, source_path, plate_id, item_id)
        if prime is not None:
            steps.append(prime)
        blowoff = await _plate_blowoff_step(db, source_path, plate_id, item_id)
        if blowoff is not None:
            steps.append(blowoff)
        if gcode_injection:
            snippets = await _snippet_step(db, item_id, printer_model)
            if snippets is not None:
                steps.append(snippets)

        if not steps:
            return DispatchFile(None, "unmodified")

        stack = tuple(steps)
        fingerprint = "\n".join(step.fingerprint for step in stack)
        result = await derived_3mf_cache.get_or_build(
            source_path,
            plate_id,
            fingerprint,
            lambda: transform_plate_gcode(source_path, plate_id, _fold(stack)),
            namespace=_NAMESPACE,
            max_bytes=_MAX_BYTES,
        )
        if isinstance(result, derived_3mf_cache.BuildFailed):
            logger.error(
                "%s item %s: derived file build FAILED for plate %s of %s (%s) — uploading the original",
                _SEAM_LOG,
                item_id,
                plate_id,
                source_path.name,
                result.detail,
            )
            return DispatchFile(None, "build_failed")
        return DispatchFile(result.path, "derived")
    except Exception:  # noqa: BLE001 — the seam never raises into dispatch
        logger.exception(
            "%s item %s: dispatch-file build raised for plate %s of %s — uploading the original",
            _SEAM_LOG,
            item_id,
            plate_id,
            source_path,
        )
        return DispatchFile(None, "build_failed")
