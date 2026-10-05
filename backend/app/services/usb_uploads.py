"""Which printers have an FTPS upload in flight right now — the one registry every upload lane writes.

A LEAF module: it imports nothing from the farm services, so every lane can register an upload
without reaching the modules that READ the registry. Its readers:

* ``usb_storage.usb_present`` and ``usb_storage.verify_and_alert_usb_drop`` — H2S firmware
  transiently reports ``sdcard=false`` for ~1 s while an upload runs, which is a dispatch blip, not
  a missing stick (a genuinely missing USB instead makes the upload FAIL, and that lane notifies);
* ``usb_storage``'s sweep — nothing leaves a drive something is being written to.

Its writers are the upload lanes — dispatch (``print_scheduler``), the remote eject
(``eject.remote``) and firmware (``firmware_update``) — each wrapping its FTPS upload in
:func:`upload_in_flight`. The registry lived in ``usb_storage`` until 2026-10-05; the sweep then
needed the eject lane's name rule and the keep-set owner, both of which reach the eject lane,
which imported ``usb_storage`` for this context manager — a cycle with no owner.

In-memory by design: an upload is a live operation of THIS process, so a restart has none in
flight and an empty set is the truth.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

# Printers with an FTPS upload in progress. Written only by ``upload_in_flight``.
_uploads_in_flight: set[int] = set()


def _reset_state() -> None:
    """Test hook: forget every registered upload."""
    _uploads_in_flight.clear()


@contextlib.asynccontextmanager
async def upload_in_flight(printer_id: int) -> AsyncIterator[None]:
    """Register an FTPS upload to ``printer_id`` for the duration of the block.

    Always deregisters on exit, including when the wrapped upload raises.
    """
    _uploads_in_flight.add(printer_id)
    try:
        yield
    finally:
        _uploads_in_flight.discard(printer_id)


def upload_active(printer_id: int) -> bool:
    """Is an FTPS upload to ``printer_id`` in progress right now? O(1), no I/O."""
    return printer_id in _uploads_in_flight
