"""A queue unit's DISPATCH TARGET — the one value object over "where may this print run?".

**The four kinds, and the columns that spell them.** A ``PrintQueueItem`` names its
target in exactly one of four ways, and :func:`target_of` is the only place that
decides which:

* ``PINNED`` — ``printer_id`` set, no pool target. An operator named the machine, so
  the scheduler never re-searches: the unit waits for that printer and no other.
* ``MODEL`` — ``target_model`` set. Any active, non-quarantined printer of that model.
* ``PRINTERS`` — ``target_printer_ids`` set. Any active, non-quarantined printer whose
  id is IN the set: a subset of the fleet treated as a POOL.
* ``UNASSIGNED`` — nothing set. Never dispatched; the upstream shape, kept because
  upstream rows still arrive in it.

(``MODEL`` and ``PRINTERS`` describe MEMBERSHIP only. "Active", "non-quarantined",
"idle", "capable" are the scheduler's own filters and are deliberately not part of
this object — :meth:`DispatchTarget.printer_filter` narrows to the target's members
and the caller ANDs its own liveness predicates onto that.)

**The pending-row invariant.** On a ``pending`` row ``printer_id`` is an operator PIN
and nothing else. The scheduler's chosen printer for a POOL unit is not written to the
row while it waits — it rides the dispatch plan and lands on the row only at the
``pending → printing`` claim, ``queue_transitions.claim_pending_for_dispatch``, which
is exactly how the decided ``ams_mapping`` already rides (2026-08-12 pin contract:
"never a cache"). The mirror, ``queue_transitions.release_unstarted_claim``, sends a
pool row back to the pool by clearing ``printer_id`` again, while a pinned row keeps
its pin. So a ``printer_id`` on a pool row is only ever a dispatch RECORD, never an
instruction — and a pool row can never be mistaken for a pinned one on a later tick.

**Canonical form is a DOMAIN rule owned here; the codec only serialises it.** A
printer-id set is deduped and SORTED because it is a SET — two rows naming the same
three printers must be byte-identical in storage, or the SJF query's ``ORDER BY``
groups them apart and every equality test over the column silently becomes an
ordering test. :func:`encode_printer_ids` applies that rule; :func:`decode_printer_ids`
is its tolerant inverse. Nothing else in the codebase may parse or write the column.

**Why the column is ``Text`` and not ``mapped_column(JSON)``.** The scheduler's SJF
pending query ``ORDER BY``s the target columns to group like-targeted units together
(``print_scheduler.py``: ``.order_by(PrintQueueItem.printer_id, PrintQueueItem.target_model, …)``),
and PostgreSQL's ``json`` type has no ordering operator at all — an ``ORDER BY`` over
it is a hard error, not a slow query. ``api_key.printer_ids`` is the JSON precedent in
this fork and is deliberately NOT followed here: nothing ever sorts or groups by it.
Storing the canonical JSON *text* keeps one representation that both engines can sort,
compare and index, and puts the structure in this module rather than in the dialect.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from sqlalchemy import ColumnElement, false, func

from backend.app.models.printer import Printer
from backend.app.utils.printer_models import normalize_printer_model, normalize_printer_model_id

logger = logging.getLogger(__name__)


class TargetKind(str, Enum):
    """Which of the four target shapes a queue row carries. Total and exclusive."""

    PINNED = "pinned"  # printer_id set, no pool target -> operator intent, never re-searched
    MODEL = "model"  # target_model set -> any active, non-quarantined printer of that model
    PRINTERS = "printers"  # target_printer_ids set -> any active, non-quarantined printer IN the set
    UNASSIGNED = "unassigned"  # nothing set -> never dispatched (upstream shape, kept)


class HasTargetColumns(Protocol):
    """The three columns :func:`target_of` reads, and nothing else.

    A Protocol rather than ``PrintQueueItem`` so this module never imports the queue
    model: the dependency would run models -> services -> models, and the object under
    discussion is a set of three column VALUES, not a row. A schema, a plan dict
    wrapper or a test double that exposes the three attributes is just as valid an
    argument as an ORM instance.
    """

    printer_id: int | None
    target_model: str | None
    target_printer_ids: str | None


@dataclass(frozen=True)
class DispatchTarget:
    """One unit's dispatch target: a kind plus exactly the payload that kind owns.

    Frozen because a target is a VALUE — two rows naming the same pool are the same
    target, and nothing may narrow one in place. The fields not belonging to
    :attr:`kind` are always empty; the constructors below are what guarantee that, so
    prefer them over calling the generated ``__init__`` directly.

    Deliberately NOT carried here: which printer a dispatch actually ran on. That is
    the row's own ``printer_id`` read as a RECORD (see the module docstring's pending-row
    invariant), and folding it in would make a printing pool row indistinguishable from
    a pinned one.
    """

    kind: TargetKind
    printer_id: int | None = None  # PINNED only
    model: str | None = None  # MODEL only — canonical short form ("H2S"), never a display name
    printer_ids: frozenset[int] = frozenset()  # PRINTERS only — deduped; ENCODED sorted

    # --- constructors -------------------------------------------------------
    # Named rather than a single validating ``__init__`` so a caller states which
    # kind it means and the shape check belongs to that statement. ``for_model`` is
    # spelled with the prefix because a frozen dataclass cannot carry both a field
    # ``model`` and a classmethod ``model`` — the class body's later binding would
    # become the FIELD's default, and every ``.model`` read would return a bound method.

    @classmethod
    def pinned(cls, printer_id: int) -> DispatchTarget:
        """An operator's named printer. The scheduler never re-searches a pin."""
        if not isinstance(printer_id, int) or isinstance(printer_id, bool):
            raise ValueError(f"A pinned target needs a printer id, got {printer_id!r}")
        return cls(kind=TargetKind.PINNED, printer_id=printer_id)

    @classmethod
    def for_model(cls, name: str) -> DispatchTarget:
        """Any printer of *name*. Stores the value AS GIVEN — callers normalise (see :meth:`for_run`)."""
        if not name or not name.strip():
            raise ValueError("A model target needs a model name")
        return cls(kind=TargetKind.MODEL, model=name)

    @classmethod
    def printers(cls, ids: Iterable[int]) -> DispatchTarget:
        """Any printer in *ids* — the POOL. An empty set is a ValueError, never an
        UNASSIGNED unit: "a subset of no printers" is a caller bug, and minting a
        never-dispatchable row out of it would hide that bug in the queue."""
        members = frozenset(int(pid) for pid in ids)
        if not members:
            raise ValueError("A printers target needs at least one printer id")
        return cls(kind=TargetKind.PRINTERS, printer_ids=members)

    @classmethod
    def unassigned(cls) -> DispatchTarget:
        """No target at all. Never dispatched — the upstream shape, kept."""
        return cls(kind=TargetKind.UNASSIGNED)

    @classmethod
    def for_run(cls, *, printer_ids: Sequence[int] | None, target_model: str | None) -> DispatchTarget:
        """The target a ``RunCreate`` asks for: printer subset XOR model.

        The model normalisation is REPLICATED from ``production_run.create_production_run``
        (``normalize_printer_model(raw) or normalize_printer_model_id(raw) or raw``) and
        both helpers are imported for that reason alone: a run's stored ``target_model``
        and this object's must be the same string, or a target created one way stops
        matching a printer the other way would have found.

        Neither given is a ValueError. The request schema already forbids it, so a
        service reaching here with nothing to target has lost its input somewhere —
        silently minting UNASSIGNED would put a unit in the queue that can never run.
        """
        ids = list(printer_ids or [])
        if ids:
            return cls.printers(ids)
        raw = target_model or ""
        if not raw.strip():
            raise ValueError("A run target needs printer_ids or target_model")
        return cls.for_model(normalize_printer_model(raw) or normalize_printer_model_id(raw) or raw)

    @classmethod
    def from_plan(cls, plan: Mapping[str, Any]) -> DispatchTarget:
        """The target recorded in a run's ``first_article_plan`` JSON.

        Same two keys and the same precedence as :meth:`for_run` — ``printer_ids``
        (list or None) then ``target_model`` (str or None) — but ``target_model`` was
        normalised when the plan was BUILT (``farm_policy.build_first_article_plan``
        stores ``production_run``'s already-normalised value), so it passes through
        unchanged. Re-normalising a stored value is how a second spelling gets minted.
        """
        raw_ids = plan.get("printer_ids")
        ids = list(raw_ids) if isinstance(raw_ids, list) else []
        if ids:
            return cls.printers(ids)
        stored_model = plan.get("target_model")
        if not isinstance(stored_model, str) or not stored_model.strip():
            raise ValueError("A first-article plan target needs printer_ids or target_model")
        return cls.for_model(stored_model)

    # --- reading ------------------------------------------------------------

    @property
    def is_pool(self) -> bool:
        """True when the scheduler may CHOOSE the printer — MODEL or PRINTERS."""
        return self.kind in (TargetKind.MODEL, TargetKind.PRINTERS)

    def fields(self) -> dict[str, Any]:
        """The three target columns, ALL THREE always present, non-owned ones None.

        Spread this LAST into a ``PrintQueueItem`` field dict: the point of returning
        the empty columns rather than only the owned one is that a caller building a
        row from a template cannot leave a previous target's column standing beside
        the new one, which is the only way a row can end up claiming two kinds.
        """
        return {
            "printer_id": self.printer_id if self.kind is TargetKind.PINNED else None,
            "target_model": self.model if self.kind is TargetKind.MODEL else None,
            "target_printer_ids": (encode_printer_ids(self.printer_ids) if self.kind is TargetKind.PRINTERS else None),
        }

    def matches(self, printer_id: int, printer_model: str | None) -> bool:
        """Is this printer a member of the target? Membership only — see the class docstring.

        The MODEL comparison is the scheduler's own (normalise the TARGET, compare
        case-insensitively against the printer's stored model): both
        ``print_scheduler._find_idle_printer_for_target`` (through :meth:`printer_filter`)
        and ``farm_correlation.farm_work_slated_for`` (through this method) ask it here,
        so the two answers cannot drift. A printer with no model recorded matches NOTHING: an unknown model
        is not a wildcard, and treating it as one would dispatch a model-targeted unit
        onto a machine nobody has identified.
        """
        if self.kind is TargetKind.PRINTERS:
            return printer_id in self.printer_ids
        if self.kind is TargetKind.MODEL:
            normalised = normalize_printer_model(self.model) or self.model or ""
            return bool(normalised) and normalised.lower() == (printer_model or "").strip().lower()
        if self.kind is TargetKind.PINNED:
            return printer_id == self.printer_id
        return False

    def printer_filter(self) -> ColumnElement[bool]:
        """The SAME predicate as :meth:`matches`, expressed as SQL over ``Printer``.

        One predicate, two dialects — the Python and SQL membership tests must never
        drift. Before this object existed the deep-park's Python model comparison and
        the scheduler's SQL one were two hand-kept mirrors, with a docstring warning
        that no SQL-side normalisation existed to keep them honest. Here both spellings
        live in one class, side by side, and a test
        asserts they select the same printers.

        UNASSIGNED renders ``false()`` rather than raising: a caller filtering a query
        by a target it did not check is asking "which printers may run this?", and for
        an unassigned unit the honest answer is "none", not an exception at query-build
        time.
        """
        if self.kind is TargetKind.PRINTERS:
            return Printer.id.in_(sorted(self.printer_ids))
        if self.kind is TargetKind.MODEL:
            normalised = normalize_printer_model(self.model) or self.model or ""
            return func.lower(Printer.model) == normalised.lower()
        if self.kind is TargetKind.PINNED:
            return Printer.id == self.printer_id
        return false()

    def label(self, names: Mapping[int, str] | None = None) -> str:
        """The target's members as a bare list: ``"H2S"``, ``"001-H2S, 003-H2S"``, ``""``.

        *names* maps printer id -> display name; a member missing from it falls back to
        ``#id`` rather than vanishing, because a target that silently lists fewer
        printers than it holds is worse than an ugly one. Members are listed in ID
        order so the same pool always reads the same way.
        """
        lookup = names or {}
        if self.kind is TargetKind.MODEL:
            return self.model or ""
        if self.kind is TargetKind.PRINTERS:
            return ", ".join(lookup.get(pid) or f"#{pid}" for pid in sorted(self.printer_ids))
        if self.kind is TargetKind.PINNED and self.printer_id is not None:
            return lookup.get(self.printer_id) or f"#{self.printer_id}"
        return ""

    def describe(self, names: Mapping[int, str] | None = None) -> str:
        """The human noun phrase for notifications and log lines.

        ``"Any H2S"`` / ``"Any of 001-H2S, 003-H2S"`` / ``"001-H2S"`` / ``"Unassigned"``.
        The "Any" prefix is what tells an operator the unit is not waiting for one
        machine — the single fact a pool unit's status line has to carry.
        """
        if self.kind is TargetKind.MODEL:
            return f"Any {self.label(names)}"
        if self.kind is TargetKind.PRINTERS:
            return f"Any of {self.label(names)}"
        if self.kind is TargetKind.PINNED:
            return self.label(names)
        return "Unassigned"


def target_of(item: HasTargetColumns) -> DispatchTarget:
    """The dispatch target of a queue row. TOTAL — every row has one, never None.

    Precedence: a non-empty decoded ``target_printer_ids`` -> PRINTERS; else a
    non-blank ``target_model`` -> MODEL; else a ``printer_id`` -> PINNED; else
    UNASSIGNED.

    **A row carrying BOTH a pool target and a ``printer_id`` is STILL the pool kind.**
    That combination is not a contradiction and must not be read as a pin: it is either
    a ``printing``/terminal row, where ``printer_id`` is the RECORD of the printer the
    dispatch ran on (written at the claim — see the module docstring), or a pre-cutover
    row whose pool pick was written at creation time. Reading it as PINNED would pin
    the fleet's history to whichever printer happened to serve a unit once.
    """
    pool_ids = decode_printer_ids(item.target_printer_ids)
    if pool_ids:
        return DispatchTarget(kind=TargetKind.PRINTERS, printer_ids=pool_ids)
    model = item.target_model
    if model and model.strip():
        return DispatchTarget(kind=TargetKind.MODEL, model=model)
    if item.printer_id is not None:
        return DispatchTarget(kind=TargetKind.PINNED, printer_id=item.printer_id)
    return DispatchTarget(kind=TargetKind.UNASSIGNED)


def encode_printer_ids(ids: Iterable[int] | None) -> str | None:
    """Serialise a printer-id set to its ONE canonical form: sorted, deduped, no spaces.

    ``[3, 1, 3, 5]`` -> ``"[1,3,5]"``. Empty or None -> ``None``, and NEVER ``"[]"``:
    an empty JSON array and NULL would be two spellings of "no pool", and the fork has
    already paid for that once — ``core.database._migrate_normalize_printer_ids`` is a
    repair migration that exists solely to rewrite ``api_keys.printer_ids = '[]'`` to
    NULL, because the two spellings had drifted apart in the code that read them. The
    empty form does not exist here, so no reader can disagree about it.
    """
    if ids is None:
        return None
    unique = sorted({int(pid) for pid in ids})
    if not unique:
        return None
    return json.dumps(unique, separators=(",", ":"))


def decode_printer_ids(text: str | None) -> frozenset[int]:
    """Parse the column back to a set of ids. Tolerant: never raises, never partial.

    ``None``, ``""``, ``"[]"``, JSON that will not parse, a non-list, or a list holding
    anything that is not an ``int`` all answer ``frozenset()`` — an empty answer, which
    :func:`target_of` reads as "no pool target" and which therefore fails CLOSED (the
    unit falls through to its pin or to UNASSIGNED and waits, visibly, rather than
    dispatching against a half-understood set).

    The rejection is WHOLE-VALUE on purpose: this column has exactly one writer
    (:func:`encode_printer_ids`), so a member that is not an int means the value did
    not come from that writer, and honouring the members that happen to look right
    would dispatch against a pool nobody wrote. Malformed input is logged at DEBUG —
    it is a data question, not a runtime failure, and the caller's own outcome (a unit
    that does not dispatch) is what surfaces it.
    """
    if not text or not text.strip():
        return frozenset()
    try:
        raw = json.loads(text)
    except (TypeError, ValueError):
        logger.debug("decode_printer_ids: not JSON, read as no pool target: %r", text)
        return frozenset()
    if not isinstance(raw, list):
        logger.debug("decode_printer_ids: not a JSON list, read as no pool target: %r", text)
        return frozenset()
    # ``bool`` is a subclass of ``int``; True in this column is corruption, not id 1.
    if any(isinstance(member, bool) or not isinstance(member, int) for member in raw):
        logger.debug("decode_printer_ids: non-int member, read as no pool target: %r", text)
        return frozenset()
    return frozenset(raw)
