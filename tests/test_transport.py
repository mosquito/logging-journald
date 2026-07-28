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


def test_a_journald_that_is_not_listening_raises(journald: FakeJournald) -> None:
    """
    The failure has to reach the caller as itself. Falling back to a file descriptor
    for every OSError hid the reason: with nothing listening, the error raised came
    from sendmsg in the fallback, not from the send that actually failed.
    """
    transport = JournaldTransport(socket_path=journald.path)
    journald.close()
    os.unlink(journald.path)

    with pytest.raises(OSError) as err:
        transport.send([("message", "nobody is listening")])

    assert err.value.errno == errno.ECONNREFUSED
