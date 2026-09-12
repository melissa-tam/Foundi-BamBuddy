"""The operator's STOP, as ONE verb.

``stop_as_operator`` is the printer-card stop: send ``print.stop`` over MQTT **and**
mark the printer user-stopped, in that order, with nothing awaited in between. The
pair is what makes the resulting terminal an operator CANCEL rather than a failure —
``main.on_print_complete`` reads the mark (via ``farm_correlation.classify_stop``'s
``operator_ui`` verdict) and rewrites the firmware's ``failed``/``aborted`` status to
``cancelled``, which is what keeps a stop out of the retry/quarantine accounting.

It exists because that pair had THREE call sites and was about to get a fourth: the
printer-card route (``api/routes/printers.py``), the queue-page route
(``api/routes/print_queue.py``) and now the service-hold quiesce, which stops whatever
is running before a human puts their hands in the machine. Each copy carried its own
spelling of the mark's lazy import and its own log line, and a fourth copy is how the
mark eventually goes missing on the path that needs it most.

What it deliberately does NOT own: the queue row's transitions (status, ``stop_source``,
``error_message``), the smart-plug auto-off and the HTTP shapes. Those differ per
caller and stay with the caller — this module is the WIRE act plus the mark, nothing
else.

**Never raises**, and returns whether the stop was DELIVERED. A caller that needs to
report undelivered (the printer-card route's 502) reads the bool; the quiesce reports it
in its verdict. ``printer_manager.stop_print`` answers False for a printer with no live
client, so "no session" and "publish refused" are one answer here — they are one answer
to the operator too.
"""

from __future__ import annotations

import logging

from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)


def stop_as_operator(printer_id: int) -> bool:
    """Stop the current print AS THE OPERATOR. True iff the stop was delivered.

    Sync by design: the mark must be set before the caller's first ``await``, because
    the MQTT terminal callback can fire during the next yield and the mark is what it
    reads (the W4b ordering — see ``print_queue``'s stop route). A coroutine here would
    re-open that window at every call site.
    """
    delivered = False
    try:
        delivered = printer_manager.stop_print(printer_id)
    except Exception:  # noqa: BLE001 — a stop is best-effort; the mark below still applies
        logger.exception("print_control: stop_print raised for printer %s", printer_id)
    if not delivered:
        logger.warning(
            "print_control: printer %s stop NOT delivered (no live MQTT session or publish refused)", printer_id
        )

    # ``mark_printer_stopped_by_user`` lives in ``main``, which imports every route
    # module and therefore this one — so the import can only be a CALL-TIME one, the
    # same lazy shape both stop routes have always used. Guarded because losing the
    # mark mislabels a cancel as a failure, which is a worse outcome than a log line
    # but not a reason to fail the stop itself.
    try:
        from backend.app.main import mark_printer_stopped_by_user

        mark_printer_stopped_by_user(printer_id)
    except Exception as mark_err:  # noqa: BLE001
        logger.warning("print_control: failed to mark printer %s as user-stopped: %s", printer_id, mark_err)
    return delivered
