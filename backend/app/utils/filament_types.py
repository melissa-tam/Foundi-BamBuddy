"""Filament-type equivalence + canonicalisation (single source of truth).

Some Bambu Lab filament types are interchangeable on the printer side — the
firmware treats them as the same material for loading/printing purposes. Both
the print scheduler (model-based filament matching) and the farm capability gate
must classify a loaded/required type identically, so the equivalence table and
the canonicaliser live here instead of being duplicated per consumer.
"""

from __future__ import annotations

# Types within the same group are interchangeable on the printer side (Bambu Lab
# firmware treats them as compatible). Add a new row to extend the equivalence.
FILAMENT_TYPE_GROUPS: list[list[str]] = [
    ["PA-CF", "PA12-CF", "PAHT-CF"],
]

# Reverse lookup: every member type (uppercased) → the group's canonical type.
_FILAMENT_EQUIV_MAP: dict[str, str] = {}
for _group in FILAMENT_TYPE_GROUPS:
    _canonical = _group[0].upper()
    for _t in _group:
        _FILAMENT_EQUIV_MAP[_t.upper()] = _canonical


def canonical_filament_type(ftype: str) -> str:
    """Return the canonical (equivalence-collapsed, uppercased) type for matching."""
    upper = ftype.upper()
    return _FILAMENT_EQUIV_MAP.get(upper, upper)
