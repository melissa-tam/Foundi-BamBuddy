"""Eject geometry accessor — resolves a printer model to its bed/envelope.

The single read path over the ``printer_model_geometry`` registry table (which
replaced the generator's in-code ``PRINTER_BED_DIMS`` / ``PRINTER_TRAVEL_ENVELOPE``
dicts). Every production surface that used to index those dicts by a raw model
string now calls one of these accessors, so:

* model spellings are canonicalised in ONE place (``canon_model``) — ``O1S`` and
  ``H2S`` resolve to the same row;
* production dispatch fails CLOSED on a model with no row, or a row whose
  ``validated`` flag is still False (envelope not through the hardware ladder),
  with a reason the operator can act on;
* the ladder tools (preview / dry-run) can opt into an unvalidated row.

``ModelGeometry`` is a frozen snapshot the pure generator/validator consume with
no DB coupling, mirroring how the dict tuples were passed before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from backend.app.models.printer_model_geometry import PrinterModelGeometry
from backend.app.utils.printer_models import canon_model

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ModelGeometry:
    """Immutable bed + travel-envelope snapshot for one printer model.

    ``bed`` is ``(bed_x, bed_y)``; ``envelope`` is ``(x_min, x_max, y_min,
    y_max)`` — the same tuple shapes the generator/validator consumed from the
    old ``PRINTER_BED_DIMS`` / ``PRINTER_TRAVEL_ENVELOPE`` dicts, so the pure
    G-code path is unchanged byte-for-byte.
    """

    model_key: str
    bed: tuple[float, float]
    envelope: tuple[float, float, float, float]
    max_part_height_mm: float
    validated: bool


class GeometryUnavailable(Exception):
    """No usable eject geometry for a model (missing row, or unvalidated when
    validation was required). ``reason`` is an operator-facing message; the two
    causes are distinguished in the text so the UI can guide the next step."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _to_geometry(row: PrinterModelGeometry) -> ModelGeometry:
    return ModelGeometry(
        model_key=row.model_key,
        bed=(row.bed_x, row.bed_y),
        envelope=(row.env_x_min, row.env_x_max, row.env_y_min, row.env_y_max),
        max_part_height_mm=row.max_part_height_mm,
        validated=bool(row.validated),
    )


async def get_geometry(db: AsyncSession, model: str | None) -> ModelGeometry | None:
    """Return the :class:`ModelGeometry` for ``model``, or ``None`` if no row.

    ``model`` is canonicalised (``canon_model``) before lookup, so any spelling
    (internal code / display name / short name) resolves to the same registry
    row. Returns ``None`` for a blank model or a model with no geometry row —
    callers that must not proceed without geometry use
    :func:`get_geometry_required` instead.
    """
    key = canon_model(model)
    if key is None:
        return None
    result = await db.execute(select(PrinterModelGeometry).where(PrinterModelGeometry.model_key == key))
    row = result.scalar_one_or_none()
    return _to_geometry(row) if row is not None else None


async def get_geometry_required(db: AsyncSession, model: str | None, *, require_validated: bool) -> ModelGeometry:
    """Return the geometry for ``model`` or raise :class:`GeometryUnavailable`.

    Fail-closed resolver for every dispatch surface. Raises with a reason that
    distinguishes the two failure causes:

    * no row for the (canonicalised) model → ``"no eject geometry for model X"``;
    * ``require_validated`` and the row's ``validated`` is False →
      ``"geometry for X is not hardware-validated — run the hardware ladder"``.

    Production paths pass ``require_validated=True`` (a model only ejects
    unattended once its envelope has been through the ladder). The ladder tools
    pass ``require_validated=False`` to build against a provisional envelope.
    """
    geometry = await get_geometry(db, model)
    if geometry is None:
        key = canon_model(model)
        label = key or (model or "<unknown>")
        raise GeometryUnavailable(f"no eject geometry for model {label!r}")
    if require_validated and not geometry.validated:
        raise GeometryUnavailable(
            f"geometry for {geometry.model_key!r} is not hardware-validated — run the hardware ladder"
        )
    return geometry


async def list_geometries(db: AsyncSession) -> list[ModelGeometry]:
    """All geometry rows as :class:`ModelGeometry`, ordered by model key."""
    result = await db.execute(select(PrinterModelGeometry).order_by(PrinterModelGeometry.model_key))
    return [_to_geometry(row) for row in result.scalars().all()]
