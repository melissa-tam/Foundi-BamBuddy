"""The label weight a NEW spool row is minted with — ONE owner, a LEAF.

Every way a roll enters the internal inventory asks here when nothing it holds states a
figure: an untagged AMS roll (the tagless default mint, the bare-tray autoconfig, the New
roll replacement), a tagged roll whose tag reports no ``tray_weight``, the respool
fallback, Add spool (``SpoolCreate.label_weight`` left unset) and CSV import (an empty
``label_weight`` cell). The rule is one sentence — **a positive stated figure wins, else
the ``default_spool_weight_g`` setting** — and it lives in :func:`label_weight_for`.

This module is the ONLY reader of the setting key (``test_code_quality
.TestSpoolLabelWeightOwnership``), and the setting's schema default
(``AppSettings.default_spool_weight_g``) is the one origin of the fallback figure: the
``Spool`` ORM column carries no Python default, so a construction that forgets the label
fails loudly instead of minting a silent literal.

A leaf below ``spool_tag_matcher``: it imports no spool service (``spool_tagless`` imports
``spool_tag_matcher``, so the owner living in either would close a cycle). Existing rows
are never rewritten here — a row's label is its own history (operator ruling 2026-10-16).

Cost: one settings read per mint that needs the default; never per push.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.schemas.settings import AppSettings

logger = logging.getLogger(__name__)

SETTING_KEY = "default_spool_weight_g"

# The setting's own schema default and bounds — read off the field, never re-spelled.
_FIELD = AppSettings.model_fields[SETTING_KEY]
_SCHEMA_DEFAULT_G: int = int(_FIELD.default)
_BOUNDS: tuple[int, int] = (
    next(int(m.ge) for m in _FIELD.metadata if getattr(m, "ge", None) is not None),
    next(int(m.le) for m in _FIELD.metadata if getattr(m, "le", None) is not None),
)


def _stated(figure: object) -> int | None:
    """A figure as a positive whole number of grams, or None when it states nothing.

    The wire sends ``tray_weight`` as a numeric string (``"1000"``, ``"0"`` on an untagged
    tray); a form or CSV sends an int. ``0``, a negative, a blank and junk all state
    nothing. ``bool`` is rejected: a JSON ``true`` is not the number 1.
    """
    if figure is None or isinstance(figure, bool):
        return None
    try:
        grams = int(float(figure))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return grams if grams > 0 else None


def _first_stated(figures: tuple[object, ...]) -> int | None:
    for figure in figures:
        grams = _stated(figure)
        if grams is not None:
            return grams
    return None


def label_weight_for(*figures: object, default_g: int) -> int:
    """THE rule: the first positive stated figure, else ``default_g`` (the setting).

    For a caller that reads the setting once for many rows (CSV import); a single mint
    calls :func:`resolve_label_weight`.
    """
    grams = _first_stated(figures)
    return default_g if grams is None else grams


async def default_label_weight_g(db: AsyncSession) -> int:
    """The ``default_spool_weight_g`` setting, or its schema default when unset or unreadable."""
    from backend.app.api.routes.settings import get_setting

    raw = await get_setting(db, SETTING_KEY)
    grams = _stated(raw)
    if grams is None:
        return _SCHEMA_DEFAULT_G
    low, high = _BOUNDS
    if not low <= grams <= high:
        logger.warning(
            "[spool-defaults] %s=%r is outside %d-%d g; minting at the schema default %d g",
            SETTING_KEY,
            raw,
            low,
            high,
            _SCHEMA_DEFAULT_G,
        )
        return _SCHEMA_DEFAULT_G
    return grams


async def resolve_label_weight(db: AsyncSession, *figures: object) -> int:
    """The label a new row is minted with: the first positive figure, else the setting.

    Reads the setting only when no figure states one, so a tagged roll whose tag reports
    its weight costs no settings read.
    """
    grams = _first_stated(figures)
    if grams is not None:
        return grams
    return await default_label_weight_g(db)
