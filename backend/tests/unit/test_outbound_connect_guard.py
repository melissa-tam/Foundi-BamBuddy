"""The self-test for the harness's one outbound-connection rule.

The guard lives in ``backend/tests/_fixtures/netguard.py`` and is installed by
``backend/tests/conftest.py`` for the whole process. These tests pin the three
properties the rest of the suite depends on: a fake printer IP fails
*immediately* and as an ``OSError``/``TimeoutError`` (so no production handler
changes its verdict), 127.0.0.1 still connects for real (the pyftpdlib FTPS
server and the virtual-printer servers need it), and anything unclassifiable is
passed through rather than guessed at.
"""

import errno
import socket
import time

import pytest

from backend.tests._fixtures.netguard import (
    BlockedOutboundConnection,
    install_outbound_connect_guard,
    is_blocked_address,
)

# One of the addresses the suite actually seeds (backend/tests/conftest.py's
# printer_factory hands out 192.168.1.10x); 990 is the printers' FTPS port.
FAKE_PRINTER = ("192.168.1.100", 990)


def test_the_harness_installed_the_guard():
    """conftest.py — not this module's import — is what arms it."""
    assert getattr(socket.socket.connect, "_bambuddy_netguard", False)
    assert getattr(socket.socket.connect_ex, "_bambuddy_netguard", False)


def test_fake_printer_ip_is_refused_immediately():
    """The whole point: no ~21 s connect timeout, and no waiting at all."""
    started = time.monotonic()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(30)
        with pytest.raises(BlockedOutboundConnection) as caught:
            sock.connect(FAKE_PRINTER)
    assert time.monotonic() - started < 1.0

    # Faithful to the failure it replaces: production catches TimeoutError and
    # OSError, so its classification is unchanged.
    assert isinstance(caught.value, TimeoutError)
    assert isinstance(caught.value, OSError)
    assert caught.value.errno == errno.ETIMEDOUT
    assert "192.168.1.100:990" in str(caught.value)
    assert "netguard" in str(caught.value)


def test_connect_ex_reports_the_same_errno_without_raising():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        assert sock.connect_ex(FAKE_PRINTER) == errno.ETIMEDOUT


def test_loopback_still_connects_for_real():
    """test_bambu_ftp.py's FTPS server and the test_vp_* servers live here."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=2) as client:
            accepted, _ = server.accept()
            accepted.close()
            assert client.fileno() != -1


@pytest.mark.parametrize(
    "address",
    [
        ("127.0.0.1", 990),
        ("127.0.0.5", 8883),
        ("::1", 990, 0, 0),
        ("localhost", 990),
        ("printer.local", 990),
        ("", 990),
        "/tmp/some.sock",
        None,
    ],
    ids=["loopback", "loopback-range", "ipv6-loopback", "localhost", "hostname", "empty", "unix-path", "none"],
)
def test_fail_open_for_everything_not_provably_off_machine(address):
    assert is_blocked_address(address) is False


@pytest.mark.parametrize(
    "address",
    [("192.168.1.100", 990), ("1.2.3.4", 990), ("10.0.0.9", 990), ("8.8.8.8", 53), ("2001:db8::1", 990, 0, 0)],
    ids=["192.168", "1.2.3.4", "10.0.0.9", "public-v4", "public-v6"],
)
def test_blocks_every_non_loopback_ip_literal(address):
    assert is_blocked_address(address) is True


def test_install_is_idempotent():
    """A second call must not wrap the wrapper (that is how a guard grows a
    stack of frames and a re-entrancy bug)."""
    before = socket.socket.connect
    install_outbound_connect_guard()
    assert socket.socket.connect is before
