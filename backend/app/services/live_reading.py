"""Is a ``PrinterState`` evidence about the printer RIGHT NOW? — a LEAF.

Two questions, one spelling each, for every reader:

* :func:`is_fresh` — the state describes the printer's CURRENT MQTT session;
* :func:`reads_live` — that, and a ``gcode_state`` the printer has positively reported.

They lived in ``print_reconcile`` and ``spool_recovery`` until 2026-10-10, when the refill verdict
(``refill_verdict``) that composes them gained a reader inside the client registry (the status
frame's ``toolhead.refill_reason``, built by ``printer_manager``). Both of those modules sit ABOVE
the registry (``print_reconcile`` imports ``job_terminal``, which imports ``printer_manager``), so
the readers moved down to a module that reaches nothing farm-side at all
(``test_import_graph.TestTheRefillVerdictIsALeaf``). Getattr-safe and total.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.app.services.bambu_mqtt import PrinterState


def is_fresh(state: PrinterState | None) -> bool:
    """Does ``state`` describe the printer's CURRENT MQTT session? ONE spelling, for the
    connected-edge hook in ``main``, for the downtime reconcile's evidence
    (``print_reconcile.evidence_of``), for the AMS command snapshot (``ams_command.snapshot``), for
    the plate-check episode (``pause_recovery``) and for :func:`reads_live`.

    Total: no state (no client registered), or an object without the session fields, is
    not fresh — the command snapshot is getattr-safe by contract and reads through here."""
    if state is None:
        return False
    report_epoch = getattr(state, "report_epoch", None)
    return (
        bool(getattr(state, "connected", False))
        and report_epoch is not None
        and report_epoch == getattr(state, "connection_epoch", None)
    )


def reads_live(state: PrinterState | None) -> bool:
    """Is ``state`` evidence about the printer RIGHT NOW? THE recovery driver's one freshness
    question, and the refill verdict's first gate (``refill_verdict.refill_owed``).

    :func:`is_fresh` — connected, and the current session's first report APPLIED — plus the
    rearm's own evidence rule (``spool_recovery._hold_over``): a ``gcode_state`` the printer has
    not positively reported (``""`` / ``UNKNOWN``) is not a reading either. No state at all (no
    client registered — a startup before the printer connects, a re-registration) is not fresh by
    the same test.
    """
    return is_fresh(state) and (getattr(state, "state", None) or "").upper() not in ("", "UNKNOWN")
