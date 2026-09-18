"""THE harness rule that the test suite opens no outbound TCP connection.

Why this exists
---------------
205 ``ip_address="…"`` literals across 63 test files seed printers at addresses
that do not exist (``192.168.1.100``, ``1.2.3.4``, ``10.0.0.9``, …). Any test
that reaches a production path which then opens a real socket — ``bambu_ftp``
(FTPS/990 via ``ftplib``) or ``bambu_mqtt`` (paho/8883), both of which use
*blocking* sockets — pays a full connect timeout per attempt: on Windows an
unroutable address costs ~21 s, and the retry ladders stack several of them.
``backend/tests/integration/test_print_lifecycle.py`` spent 847 s that way,
almost all of it in ``socket.connect``.

The fix belongs to ONE owner, not to 205 call sites: a connect that the harness
can prove is leaving the machine fails immediately instead of timing out.

What it does and does not change
--------------------------------
The guard is *faithful*, not louder: it raises
:class:`BlockedOutboundConnection`, a ``TimeoutError`` (hence ``OSError``)
carrying ``errno.ETIMEDOUT``/``WSAETIMEDOUT`` — exactly the class the real
unroutable-address failure raises, and exactly what every production handler
already catches (``BambuFTPClient.connect`` catches ``TimeoutError`` and
``OSError``; paho treats ``OSError`` as a failed connect). So a test's verdict
is unchanged; only the 21 s wait is gone. The exception message names the
address and this module, so a developer reading a log sees who refused it.

It is deliberately **fail-open**: only an address that *parses* as a
non-loopback IP literal is refused. Loopback (``127.0.0.0/8``, ``::1``) is
allowed, which is what keeps the suites that run REAL local servers working —
``unit/services/test_bambu_ftp.py`` (pyftpdlib implicit-FTPS on 127.0.0.1),
the ``test_vp_*`` virtual-printer MQTT/FTP servers, and asyncio's own Windows
self-pipe ``socketpair``. A hostname, a UNIX path, or anything this module
cannot classify is passed straight through rather than guessed at.

Scope note: the guard sits on ``socket.socket``, so it covers blocking-socket
users (``ftplib``, ``paho-mqtt``) — which is the entire ~21 s class. The app's
HTTP is async ``httpx``, which on Windows connects through the proactor loop's
overlapped ``ConnectEx`` and never touches ``socket.connect``; it is out of
scope here and costs nothing, because those tests mock the transport.
"""

import errno
import ipaddress
import socket

__all__ = [
    "BlockedOutboundConnection",
    "install_outbound_connect_guard",
    "is_blocked_address",
]

# Windows' WSAETIMEDOUT (10060) is what an unroutable connect actually reports
# there; keep it on the exception so code that inspects ``winerror`` sees the
# same value it would in production.
_WSAETIMEDOUT = 10060


class BlockedOutboundConnection(TimeoutError):
    """A connect the harness refused because it would have left the machine.

    Subclasses ``TimeoutError`` (an ``OSError``) on purpose: production code
    already classifies an unreachable printer that way, so refusing instantly
    is indistinguishable from the 21 s timeout it replaces.
    """


def is_blocked_address(address: object) -> bool:
    """Whether ``address`` is provably a non-loopback IP literal.

    Fail-open by design — anything unparseable (a hostname, a UNIX path, an
    ``AF_BLUETOOTH`` tuple) is NOT blocked.
    """
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if not isinstance(host, str) or not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # hostname or anything else we cannot classify
    return not ip.is_loopback


def _blocked(address: tuple) -> BlockedOutboundConnection:
    exc = BlockedOutboundConnection(
        errno.ETIMEDOUT,
        f"outbound connection to {address[0]}:{address[1] if len(address) > 1 else '?'} refused by the test "
        "harness (backend/tests/_fixtures/netguard.py): tests open no real sockets off this machine. "
        "Mock the transport (bambu_ftp / usb_storage / bambu_mqtt) or point the fixture at 127.0.0.1.",
    )
    exc.winerror = _WSAETIMEDOUT
    return exc


def install_outbound_connect_guard() -> None:
    """Install the guard on ``socket.socket``. Idempotent."""
    if getattr(socket.socket.connect, "_bambuddy_netguard", False):
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def connect(self, address):
        if is_blocked_address(address):
            raise _blocked(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        if is_blocked_address(address):
            # connect_ex reports, never raises — return the same errno the
            # timeout would have produced.
            return errno.ETIMEDOUT
        return real_connect_ex(self, address)

    connect._bambuddy_netguard = True
    connect_ex._bambuddy_netguard = True
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
