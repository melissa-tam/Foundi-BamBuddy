"""How far the ENDING job got, and how far it FED — its terminal payload's extent keys, read ONCE.

The MQTT client's one peaks reader (``bambu_mqtt.JobPeaks``) writes four keys into every terminal
payload (``JobPeaks.terminal_fields``) — ``last_progress``, ``last_layer_num``, ``peaks_reliable`` and
``first_unfed_layer`` — beside the job's ``total_layers`` (``bambu_mqtt.job_consumption_evidence``).
Two carriers read them: the plate authority's ``plate_occupancy.DepositEvidence`` (has the job left
anything on the plate) and the charge's ``usage_tracker.JobEvidence`` (how far it ran, which trays fed
it). Each used to parse the keys itself; :class:`JobExtent` is now THE parse, and both carriers are
built on it (they subclass it, so each carries the one reading under the field names its readers
already use), so the plate gate, the charge basis and every gram consumer read the same numbers.

**Why a leaf of its own.** The plate authority is stdlib-only by contract (no ``backend.app`` import,
no I/O) and the usage tracker is a DB service; neither may reach into the other. A stdlib-only module
with no ``backend.app`` import keeps ``plate_occupancy``'s closure stdlib-only and gives both one
direction to depend in. It imports nothing from the farm, and never ``main``.

**A layer printed with nothing fed is not a layer fed** (011-H2S and 014-H2S, 2026-10-09: resumed onto
an empty toolhead, the jobs ran to the end on air and the printer said FINISH). ``first_unfed_layer``
is the lowest layer the job printed with its active extruder empty — absolute, so it stays valid after
an attach; ``None`` when nothing measured it (an older payload, the downtime reconcile's synthesis,
which never carries the key). :attr:`JobExtent.charged_extent` is how far a PARTIAL charge runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    # Annotation-only: this leaf imports nothing from the farm at runtime.
    from backend.app.services.terminal_outcome import ChargeBasis


def payload_int(value: object, default: int) -> int:
    """A terminal payload's integer key: ``value`` when it is a real int (never a bool), else
    ``default``. Public because the charge's own payload keys (``usage_tracker.JobEvidence``'s trays)
    are read by the same rule."""
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _float_or(value: object, default: float) -> float:
    """``value`` as a float when it is a real number (never a bool), else ``default``."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _layer_or_none(value: object) -> int | None:
    """A measured layer number, or ``None`` — a key that is absent, null or not a layer measured nothing."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _share(value: float) -> float:
    """A share of the plate, clamped into [0, 1]."""
    return max(0.0, min(value, 1.0))


@dataclass(frozen=True, slots=True)
class ChargedExtent:
    """How much of the plate a PARTIAL charge runs to.

    ``layer`` — the layer the charge runs to: the layers BELOW it fed (0 = no layer was read). The
    per-layer G-code consumers take their cumulative extrusion at it, and the per-tray split ends
    every feeder segment there.

    ``fraction`` — the share of the plate's slicer estimate, in [0, 1], for the consumers that have
    no per-layer data and scale the estimate linearly.
    """

    layer: int
    fraction: float


def plate_share(charge: ChargeBasis, extent: ChargedExtent | None) -> float:
    """THE share of the plate's slicer estimate a terminal is charged: the whole plate on a ``full``
    basis, the share the job FED on ``partial`` (``extent``, the payload's
    :attr:`JobExtent.charged_extent`), nothing on ``none``.

    Every reader that scales the slicer estimate scales it by this — the 3MF lane's linear charge,
    the archive cost's top-up, the completion notification's grams — so none of them can notify,
    price or charge a plate the job printed on air (011-H2S, 2026-10-09: fed 93 of 167 layers).
    """
    if charge == "full":
        return 1.0
    if charge == "partial" and extent is not None:
        return extent.fraction
    return 0.0


class JobExtentFields(TypedDict):
    """:class:`JobExtent`'s fields as keyword arguments, typed, so every carrier's construction from
    :meth:`JobExtent.payload_fields` is checked against them."""

    peaks_reliable: bool
    last_layer_num: int
    last_progress: float
    total_layers: int
    first_unfed_layer: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class JobExtent:
    """The terminal payload's extent keys, read once (:meth:`payload_fields`). A key the payload lacks
    reads as "not observed" — no progress, no layers, not a measurement, nothing unfed — never a guess.

    * ``peaks_reliable`` — the client watched the job START, so its peaks are a measurement (an
      attach mid-job is not; ``plate_occupancy.DepositEvidence`` fails closed on that).
    * ``last_layer_num`` / ``last_progress`` — the job's peaks, its last valid reading before the
      terminal zeroed the live fields.
    * ``total_layers`` — the slicer's layer count (0 = unknown), kept across the firmware's
      end-of-print reset.
    * ``first_unfed_layer`` — the lowest layer the job printed with nothing fed, or ``None``.
    """

    peaks_reliable: bool = False
    last_layer_num: int = 0
    last_progress: float = 0.0
    total_layers: int = 0
    first_unfed_layer: int | None = None

    @property
    def printed_unfed(self) -> bool:
        """Did the job print layers with NOTHING fed — a measured ``first_unfed_layer`` below its last
        layer (the slicer's count, else the highest layer it reached)? A layer at the end is the
        end-of-print retract, never printing on air; with no last layer known nothing proves either."""
        last_layer = self.total_layers or self.last_layer_num
        return self.first_unfed_layer is not None and self.first_unfed_layer < last_layer

    @property
    def charged_extent(self) -> ChargedExtent:
        """How far a PARTIAL charge runs — the extent the job FED.

        * nothing measured unfed: the job's peaks, as before — its last layer and its last percent;
        * a measured ``first_unfed_layer``: to that layer (never past the last layer read), and the
          plate share it is of the slicer's layer count. The share is bounded by the last percent
          too when one was read — two readings of how far the job got, and the fed share can exceed
          neither; with no layer count, the percent per layer reached scales it; with neither, there
          is nothing to scale by and the linear share is 0 (the G-code cumulative still reads the
          layer). When in doubt, less: the ledger heals an under-charge from the wire, never an
          over-charge (doctrine rule 8).
        """
        by_progress = _share(self.last_progress / 100.0)
        if self.first_unfed_layer is None:
            return ChargedExtent(layer=self.last_layer_num, fraction=by_progress)
        layer = min(self.first_unfed_layer, self.last_layer_num) if self.last_layer_num > 0 else self.first_unfed_layer
        if self.total_layers > 0:
            by_layer = _share(layer / self.total_layers)
        elif self.last_layer_num > 0:
            by_layer = by_progress * _share(layer / self.last_layer_num)
        else:
            by_layer = 0.0
        fraction = min(by_layer, by_progress) if self.last_progress > 0 else by_layer
        return ChargedExtent(layer=layer, fraction=fraction)

    @staticmethod
    def payload_fields(data: Mapping[str, object]) -> JobExtentFields:
        """THE parse of the extent keys, as the keyword arguments every carrier is built from."""
        return {
            "peaks_reliable": bool(data.get("peaks_reliable", False)),
            "last_layer_num": payload_int(data.get("last_layer_num"), 0),
            "last_progress": _float_or(data.get("last_progress"), 0.0),
            "total_layers": payload_int(data.get("total_layers"), 0),
            "first_unfed_layer": _layer_or_none(data.get("first_unfed_layer")),
        }
