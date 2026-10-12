"""Route the RECORD's publishes (``services/printer_action``) to registry-shaped test doubles.

Before 2026-10-11 the farm lanes published through ``printer_manager.start_print(printer_id, file,
**options)`` / ``stop_print(printer_id)``, and their tests patched those two registry methods. The
methods are gone: every state-changing command goes through the record, which INSERTs its row and
publishes on the registry's client with the row id as the sequence id. :func:`route_verbs` keeps
those tests' doubles meaningful without a prod seam: it hands the record a client whose
``start_print`` / ``stop_print`` call the double in the OLD shape — ``start(printer_id, file,
**options)``, ``stop(printer_id)`` — and delegates every other attribute to the client the registry
would have returned (or a connected ``PrinterState`` when there is none). Nested routes chain.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from unittest.mock import patch

from backend.app.services import printer_action
from backend.app.services.bambu_mqtt import PrinterState


class _RoutedClient:
    def __init__(
        self, printer_id: int, base: object, start: Callable | None, stop: Callable | None, drying: Callable | None
    ) -> None:
        self._printer_id = printer_id
        self._base = base
        self._start = start
        self._stop = stop
        self._drying = drying

    @property
    def state(self) -> object:
        state = getattr(self._base, "state", None) if self._base is not None else None
        if state is None:
            state = PrinterState()
            state.connected = True
        return state

    def start_print(self, filename: str, *args: object, sequence_id: str, **options: object) -> object:
        if self._start is None:
            return self._base.start_print(filename, *args, sequence_id=sequence_id, **options)  # type: ignore[union-attr]
        return self._start(self._printer_id, filename, *args, **options)

    def stop_print(self, *, sequence_id: str) -> object:
        if self._stop is None:
            return self._base.stop_print(sequence_id=sequence_id)  # type: ignore[union-attr]
        return self._stop(self._printer_id)

    def send_drying_command(
        self,
        ams_id: int,
        temp: int,
        duration: int,
        mode: int = 1,
        filament: str = "",
        rotate_tray: bool = False,
        *,
        sequence_id: str,
    ) -> object:
        if self._drying is None:
            return self._base.send_drying_command(  # type: ignore[union-attr]
                ams_id, temp, duration, mode, filament, rotate_tray, sequence_id=sequence_id
            )
        if mode == 0:
            return self._drying(self._printer_id, ams_id, temp=0, duration=0, mode=0)
        return self._drying(self._printer_id, ams_id, temp, duration, mode=mode, filament=filament)

    def __getattr__(self, name: str) -> object:
        return getattr(self._base, name)


class _RoutedManager:
    def __init__(self, inner: object, start: Callable | None, stop: Callable | None, drying: Callable | None) -> None:
        self._inner = inner
        self._start = start
        self._stop = stop
        self._drying = drying

    def get_client(self, printer_id: int) -> _RoutedClient:
        base = self._inner.get_client(printer_id)  # type: ignore[attr-defined]
        return _RoutedClient(printer_id, base, self._start, self._stop, self._drying)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


@contextmanager
def route_verbs(
    *, start: Callable | None = None, stop: Callable | None = None, drying: Callable | None = None
) -> Iterator[Callable | None]:
    """For the duration: the record's starts call ``start(printer_id, filename, **options)``, its
    stops ``stop(printer_id)`` and its drying commands ``drying(printer_id, ams_id, ...)`` in the
    scheduler's old registry shape; their return value is whether the frame went out. Yields the double it
    routes (``start``, else ``stop``), so ``with route_verbs(start=m) as start:`` reads like the patch
    it replaced."""
    with patch.object(
        printer_action, "printer_manager", _RoutedManager(printer_action.printer_manager, start, stop, drying)
    ):
        yield start if start is not None else stop


def route_verbs_mp(
    monkeypatch, *, start: Callable | None = None, stop: Callable | None = None, drying: Callable | None = None
) -> None:
    """:func:`route_verbs` for a ``monkeypatch`` test (undone at teardown)."""
    monkeypatch.setattr(
        printer_action, "printer_manager", _RoutedManager(printer_action.printer_manager, start, stop, drying)
    )


def publish_at_once(monkeypatch, get_client: Callable[[int], object | None]) -> None:
    """For a test whose subject is a LANE, not the record: the record's ``send`` / ``send_soon``
    publish on ``get_client(printer_id)`` (or the ``client`` passed) at once under sequence id
    ``"1"`` and answer the publish, with no row written. The record's own contract (row before
    publish, the sync send's live-session hand-over) is pinned in ``test_printer_action``."""
    from types import SimpleNamespace

    async def fake_send(printer_id, command, *, actor, client=None, **_kw):
        target = client if client is not None else get_client(printer_id)
        return SimpleNamespace(published=target is not None and bool(command.publish(target, "1")))

    def fake_send_soon(printer_id, *commands, actor, **_kw):
        target = get_client(printer_id)
        if target is None:
            return False
        return all(bool(command.publish(target, "1")) for command in commands)

    monkeypatch.setattr(printer_action, "send", fake_send)
    monkeypatch.setattr(printer_action, "send_soon", fake_send_soon)
