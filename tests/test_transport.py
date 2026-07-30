import array
import errno
import os
import socket
import struct
from io import BytesIO
from pathlib import Path
from typing import Dict, Iterator

import pytest

from logging_journald import JournaldTransport


VALUE_LEN_STRUCT = struct.Struct("@Q")


def decode(data: bytes) -> Dict[str, str]:
    """Decode the native protocol the way journald reads it."""
    result = {}
    with BytesIO(data) as fp:
        line = fp.readline()
        while line:
            if b"=" not in line:
                key = line.decode().strip()
                value_len = VALUE_LEN_STRUCT.unpack(fp.read(VALUE_LEN_STRUCT.size))[0]
                value = fp.read(value_len).decode()
                assert fp.read(1) == b"\n"
            else:
                key, value = map(lambda x: x.strip(), line.decode().split("=", 1))
            result[key] = value
            line = fp.readline()
    return result


class FakeJournald:
    """A datagram socket at a path, decoding whatever is sent to it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.arrived_as = ""
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.bind(str(path))

    def receive(self) -> Dict[str, str]:
        data, ancdata, _, _ = self.socket.recvmsg(8 * 1024 * 1024, socket.CMSG_SPACE(4))
        self.arrived_as = "fd" if ancdata else "datagram"
        if ancdata:
            # An oversized entry is handed over as a sealed file descriptor. The whole
            # file description comes with it, offset included, and the sender left the
            # offset at the end of what it wrote -- journald mmaps the file, so read
            # from the beginning.
            fds = array.array("i")
            fds.frombytes(ancdata[0][2])
            with os.fdopen(fds[0], "rb") as payload:
                payload.seek(0)
                data = payload.read()
        return decode(data)

    def close(self) -> None:
        self.socket.close()


@pytest.fixture
def journald(tmp_path: Path) -> Iterator[FakeJournald]:
    server = FakeJournald(tmp_path / "socket")
    try:
        yield server
    finally:
        server.close()


def test_socket_path_argument(journald: FakeJournald) -> None:
    JournaldTransport(socket_path=journald.path).send([("message", "hello")])
    assert journald.receive() == {"MESSAGE": "hello"}


def test_socket_path_class_attribute_still_default(
    journald: FakeJournald, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overriding SOCKET_PATH keeps working, since it is the argument's default."""
    monkeypatch.setattr(JournaldTransport, "SOCKET_PATH", journald.path)

    JournaldTransport().send([("message", "hello")])
    assert journald.receive() == {"MESSAGE": "hello"}


def test_oversized_entry_is_sent_as_a_file_descriptor(journald: FakeJournald) -> None:
    message = "x" * (4 * 1024 * 1024)

    JournaldTransport(socket_path=journald.path).send([("message", message)])

    assert journald.receive() == {"MESSAGE": message}
    assert journald.arrived_as == "fd"


def test_ordinary_entry_is_sent_as_a_datagram(journald: FakeJournald) -> None:
    JournaldTransport(socket_path=journald.path).send([("message", "hello")])

    journald.receive()
    assert journald.arrived_as == "datagram"


def test_reconnects_when_the_socket_has_been_replaced(journald: FakeJournald) -> None:
    """
    Restarting journald's socket unit replaces the socket file, and a socket connected
    to the old one is dead for good -- every later send fails with ECONNREFUSED, which
    is what a long-running process looks like after `systemctl restart` (see #12).
    """
    transport = JournaldTransport(socket_path=journald.path)
    transport.send([("message", "before")])
    assert journald.receive() == {"MESSAGE": "before"}

    journald.close()
    os.unlink(journald.path)
    replacement = FakeJournald(journald.path)

    transport.send([("message", "after")])
    assert replacement.receive() == {"MESSAGE": "after"}
    replacement.close()


def test_a_journald_that_is_not_listening_raises(journald: FakeJournald) -> None:
    """
    The failure has to reach the caller as itself. Falling back to a file descriptor
    for every OSError hid the reason: with nothing listening, the error raised came
    from sendmsg in the fallback, not from the send that actually failed.

    The socket file is left in place and only the listener goes away, so reconnecting
    fails the same way and the errno stays the one that describes the problem.
    """
    transport = JournaldTransport(socket_path=journald.path)
    journald.close()

    with pytest.raises(OSError) as err:
        transport.send([("message", "nobody is listening")])

    assert err.value.errno == errno.ECONNREFUSED


def test_unconnected_transport_sends(journald: FakeJournald) -> None:
    JournaldTransport(socket_path=journald.path, connected=False).send([("message", "hello")])
    assert journald.receive() == {"MESSAGE": "hello"}


def test_unconnected_transport_needs_no_reconnect_when_the_socket_is_replaced(
    journald: FakeJournald,
) -> None:
    """
    Why the option exists. A journald that is socket-activated for a namespace exits
    when idle and its socket is recreated on the next activation, so a connected sender
    is repeatedly left holding a dead socket. Resolving the path per send, as
    sd_journal_sendv() does, makes the replacement invisible -- and without the retry,
    so a send that fails failed for its own reason.
    """
    transport = JournaldTransport(socket_path=journald.path, connected=False)
    transport.send([("message", "before")])
    assert journald.receive() == {"MESSAGE": "before"}
    original = transport.socket

    journald.close()
    os.unlink(journald.path)
    replacement = FakeJournald(journald.path)
    try:
        transport.send([("message", "after")])
        assert replacement.receive() == {"MESSAGE": "after"}
        assert transport.socket is original, "the same socket, never reconnected"
    finally:
        replacement.close()


def test_unconnected_transport_still_hands_over_an_oversized_entry(journald: FakeJournald) -> None:
    transport = JournaldTransport(socket_path=journald.path, connected=False)
    transport.send([("message", "x" * (4 * 1024 * 1024))])

    assert journald.receive() == {"MESSAGE": "x" * (4 * 1024 * 1024)}
    assert journald.arrived_as == "fd"


def test_unconnected_transport_raises_when_nobody_is_listening(journald: FakeJournald) -> None:
    transport = JournaldTransport(socket_path=journald.path, connected=False)
    journald.close()

    with pytest.raises(OSError) as err:
        transport.send([("message", "nobody is listening")])

    assert err.value.errno == errno.ECONNREFUSED


def test_connected_is_the_default(journald: FakeJournald) -> None:
    assert JournaldTransport(socket_path=journald.path).connected is True


def test_the_default_can_be_set_on_a_subclass(journald: FakeJournald) -> None:
    """How riact_tools picks it up: the socket path is set that way too."""
    class Unconnected(JournaldTransport):
        CONNECTED = False

    transport = Unconnected(socket_path=journald.path)
    assert transport.connected is False
    transport.send([("message", "hello")])
    assert journald.receive() == {"MESSAGE": "hello"}
