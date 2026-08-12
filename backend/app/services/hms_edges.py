"""Per-printer wire-HMS appearance-edge detection.

Separates edge-triggered STATE consumers from notify_dedup's level-triggered
alert re-notification window (600 s), which is the wrong axis for state
decisions. One producer (main.on_printer_status_change, once per push), many
consumers; this module carries no consumer vocabulary.

Frames are consumed only when they carry NEW wire HMS evidence
(PrinterState.hms_wire_at advanced) and the printer state is known. The first
consumed frame per printer SEEDS without edging — a code live at first sight
after a restart/reconnect never fires (restart-replay suppression). Local
hms_errors clears (new-print, clear_hms_errors) do not stamp hms_wire_at, so a
standing code across a job boundary or UI clear shows no edge.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.services.hms_errors import hms_short_code


@dataclass(frozen=True)
class HmsEdgeReport:
    """The codes that APPEARED on one consumed frame, in the three shapes consumers ask for."""

    appeared: tuple  # live hms_errors entries whose full_code appeared this frame
    appeared_full: frozenset[str]
    appeared_short: frozenset[str]


# printer_id -> (hms_wire_at of the last CONSUMED frame, that frame's live full_codes).
# Process-lifetime, matching the fork's other event-edge bookkeeping
# (``ams_presence._last_presence``, ``spool_respool._last_tray_now``).
_edge_state: dict[int, tuple[float, frozenset[str]]] = {}


def note_push(printer_id: int, state) -> HmsEdgeReport | None:
    """Consume one status push; return the codes that APPEARED, or None.

    None covers every non-edge outcome — no wire evidence yet, a frame not worth
    consuming, the seeding frame, and a consumed frame that added nothing.
    """
    wire_at = state.hms_wire_at
    if wire_at <= 0.0:
        # No push has ever carried HMS evidence for this printer: an empty
        # hms_errors here means "unobserved", not "no faults".
        return None
    if (state.state or "unknown").lower() == "unknown":
        # A fresh PrinterState defaults to state="unknown" and the connect-time
        # broadcast fires before any real report arrives. Such a frame must
        # neither seed nor edge, so it is not consumed either — seeding off it
        # would let the first REAL report's standing codes read as appearances.
        return None
    prev = _edge_state.get(printer_id)
    if prev is not None and wire_at <= prev[0]:
        # This frame's evidence was already consumed — a status push repeating an
        # unchanged hms_errors list is not a second appearance.
        return None

    live = state.hms_errors or []
    live_full = frozenset(e.full_code for e in live)
    if prev is None:
        _edge_state[printer_id] = (wire_at, live_full)
        return None

    appeared_full = live_full - prev[1]
    _edge_state[printer_id] = (wire_at, live_full)
    if not appeared_full:
        return None
    appeared = tuple(e for e in live if e.full_code in appeared_full)
    return HmsEdgeReport(
        appeared=appeared,
        appeared_full=appeared_full,
        appeared_short=frozenset(hms_short_code(e.attr, e.code) for e in appeared),
    )


def _reset_state() -> None:
    """Test hook: clear the module-level edge state between cases."""
    _edge_state.clear()
