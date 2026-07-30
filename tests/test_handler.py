import asyncio
import logging
import os
import socket
import struct
import sys
from collections import deque
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

import pytest

from logging_journald import Facility, JournaldLogHandler, JournaldTransport, check_journal_stream


REQUIRED_FIELDS = {
    "MESSAGE",
    "MESSAGE_ID",
    "MESSAGE_RAW",
    "PRIORITY",
    "SYSLOG_FACILITY",
    "CODE",
    "CODE_FUNC",
    "CODE_FILE",
    "CODE_LINE",
    "CODE_MODULE",
    "LOGGER_NAME",
    "PID",
    "PROCESS_NAME",
    "THREAD_ID",
    "THREAD_NAME",
    "RELATIVE_USEC",
    "CREATED_USEC",
}


def test_check_journal_stream(monkeypatch) -> None:
    stat = os.stat(sys.stderr.fileno())
    monkeypatch.setenv("JOURNAL_STREAM", f"{stat.st_dev}:{stat.st_ino}")
    assert check_journal_stream()

    monkeypatch.setenv("JOURNAL_STREAM", "")
    assert not check_journal_stream()

    monkeypatch.delenv("JOURNAL_STREAM")
    assert not check_journal_stream()

    monkeypatch.setenv("JOURNAL_STREAM", "0:0")
    assert not check_journal_stream()


def test_facility() -> None:
    assert Facility(0) == Facility.KERN
    assert Facility["KERN"] == Facility.KERN


@pytest.fixture
def sock_path():
    with TemporaryDirectory(dir="/tmp") as tmpdir:
        yield Path(tmpdir) / "notify.sock"


@pytest.fixture
def sock(sock_path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.bind(str(sock_path))
        yield s
    finally:
        s.close()


def test_transport_send_falls_back_to_memfd_on_oserror(sock_path, sock) -> None:
    transport = JournaldTransport(socket_path=sock_path)

    with mock.patch.object(socket.socket, "sendall", side_effect=OSError("no buffer space")):
        transport.send([("message", "fallback payload")])

    data, ancdata, _flags, _addr = sock.recvmsg(65536, socket.CMSG_SPACE(4))
    assert data == b""
    assert len(ancdata) == 1
    cmsg_level, cmsg_type, cmsg_data = ancdata[0]
    assert cmsg_level == socket.SOL_SOCKET
    assert cmsg_type == socket.SCM_RIGHTS

    (fd,) = struct.unpack("i", cmsg_data)
    with os.fdopen(fd, "rb") as fp:
        # the sender leaves the shared file offset at EOF after writing;
        # real journald seeks back to 0 itself before reading the memfd.
        fp.seek(0)
        assert fp.read() == b"MESSAGE=fallback payload\n"


@pytest.fixture
def fake_service(sock):
    class FakeJournald:
        VALUE_LEN_STRUCT = struct.Struct("@Q")

        def __init__(self, sock: socket.socket) -> None:
            self.sock = sock
            self.sock.setblocking(False)
            self.logs: deque[dict[str, Any]] = deque()
            self.condition = asyncio.Condition()
            self.reader_task: asyncio.Task[None] | None = None

        def parse_datagram(self, data: bytes) -> dict[str, Any]:
            result = {}
            with BytesIO(data) as fp:
                line = fp.readline()
                while line:
                    if b"=" not in line:
                        key = line.decode().strip()
                        value_len = self.VALUE_LEN_STRUCT.unpack(
                            fp.read(self.VALUE_LEN_STRUCT.size),
                        )[0]
                        value = fp.read(value_len).decode()
                        assert fp.read(1) == b"\n"
                    else:
                        key, value = map(
                            lambda x: x.strip(),
                            line.decode().split("=", 1),
                        )

                    result[key] = value
                    line = fp.readline()
            return result

        async def read_forever(self) -> None:
            loop = asyncio.get_running_loop()
            while True:
                data = await loop.sock_recv(self.sock, 65536)
                result = self.parse_datagram(data)
                async with self.condition:
                    self.logs.append(result)
                    self.condition.notify_all()

        async def wait_message(self, timeout: float = 5) -> dict[str, Any]:
            if self.reader_task is None:
                self.reader_task = asyncio.ensure_future(self.read_forever())

            async with self.condition:
                await asyncio.wait_for(
                    self.condition.wait_for(lambda: self.logs),
                    timeout=timeout,
                )
                return self.logs.popleft()

    return FakeJournald(sock)


@pytest.fixture
def log(sock_path, fake_service):
    logger = logging.getLogger("test")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.handlers.append(JournaldLogHandler(socket_path=sock_path))
    return logger


async def emit(fake_service, log_writer) -> dict[str, Any]:
    _, message = await asyncio.gather(
        asyncio.to_thread(log_writer),
        fake_service.wait_message(),
    )
    return message


async def test_simple_message(log, fake_service) -> None:
    def log_writer() -> None:
        log.info("Test message")

    message = await emit(fake_service, log_writer)
    assert message["MESSAGE"] == "Test message"
    assert message["MESSAGE_RAW"] == "Test message"
    assert message["PRIORITY"] == "6"
    assert message["CODE_FUNC"] == "log_writer"
    assert int(message["PID"]) == os.getpid()
    for field in REQUIRED_FIELDS:
        assert field in message


async def test_multiline_message(log, fake_service) -> None:
    def log_writer() -> None:
        log.info("Test multiline\nmessage")

    message = await emit(fake_service, log_writer)
    assert message["MESSAGE"] == "Test multiline\nmessage"
    assert message["MESSAGE_RAW"] == "Test multiline\nmessage"
    assert message["PRIORITY"] == "6"
    assert message["CODE_FUNC"] == "log_writer"
    assert int(message["PID"]) == os.getpid()
    for field in REQUIRED_FIELDS:
        assert field in message


async def test_formatted_message(log, fake_service) -> None:
    def log_writer() -> None:
        log.info(
            "Test formatted: int=%d str=%s repr=%r float=%0.1f",
            1,
            2,
            3,
            4,
        )

    message = await emit(fake_service, log_writer)
    assert message["MESSAGE"] == "Test formatted: int=1 str=2 repr=3 float=4.0"
    assert message["MESSAGE_RAW"] == "Test formatted: int=%d str=%s repr=%r float=%0.1f"
    assert message["ARGUMENTS_0"] == "1"
    assert message["ARGUMENTS_1"] == "2"
    assert message["ARGUMENTS_2"] == "3"
    assert message["ARGUMENTS_3"] == "4"
    assert message["PRIORITY"] == "6"
    assert message["CODE_FUNC"] == "log_writer"
    assert int(message["PID"]) == os.getpid()
    for field in REQUIRED_FIELDS:
        assert field in message


async def test_message_with_extra(log, fake_service) -> None:
    def log_writer() -> None:
        log.info(
            "Message with extra",
            extra={
                "foo": "bar",
            },
        )

    message = await emit(fake_service, log_writer)
    assert message["MESSAGE"] == "Message with extra"
    assert message["MESSAGE_RAW"] == "Message with extra"
    assert message["PRIORITY"] == "6"
    assert message["CODE_FUNC"] == "log_writer"
    assert message["EXTRA_FOO"] == "bar"
    assert int(message["PID"]) == os.getpid()
    for field in REQUIRED_FIELDS:
        assert field in message


async def test_warning_message(log, fake_service) -> None:
    def log_writer() -> None:
        log.warning("Warning test message")

    message = await emit(fake_service, log_writer)
    assert message["MESSAGE"] == "Warning test message"
    assert message["MESSAGE_RAW"] == "Warning test message"
    assert message["PRIORITY"] == "4"
    assert message["CODE_FUNC"] == "log_writer"
    assert int(message["PID"]) == os.getpid()
    for field in REQUIRED_FIELDS:
        assert field in message


async def test_critical_message(log, fake_service) -> None:
    def log_writer() -> None:
        log.critical("Critical test message")

    message = await emit(fake_service, log_writer)
    assert message["MESSAGE"] == "Critical test message"
    assert message["MESSAGE_RAW"] == "Critical test message"
    assert message["PRIORITY"] == "0"
    assert message["CODE_FUNC"] == "log_writer"
    assert int(message["PID"]) == os.getpid()
    for field in REQUIRED_FIELDS:
        assert field in message


async def test_error_message(log, fake_service) -> None:
    def log_writer() -> None:
        log.error("Error test message")

    message = await emit(fake_service, log_writer)
    assert message["MESSAGE"] == "Error test message"
    assert message["MESSAGE_RAW"] == "Error test message"
    assert message["PRIORITY"] == "3"
    assert message["CODE_FUNC"] == "log_writer"
    assert int(message["PID"]) == os.getpid()
    for field in REQUIRED_FIELDS:
        assert field in message


async def test_fatal_message(log, fake_service) -> None:
    def log_writer() -> None:
        log.log(logging.FATAL, "Fatal test message")

    message = await emit(fake_service, log_writer)
    assert message["MESSAGE"] == "Fatal test message"
    assert message["MESSAGE_RAW"] == "Fatal test message"
    assert message["PRIORITY"] == "0"
    assert message["CODE_FUNC"] == "log_writer"
    assert int(message["PID"]) == os.getpid()
    for field in REQUIRED_FIELDS:
        assert field in message


async def test_debug_message(log, fake_service) -> None:
    def log_writer() -> None:
        log.debug("Debug test message")

    message = await emit(fake_service, log_writer)
    assert message["MESSAGE"] == "Debug test message"
    assert message["MESSAGE_RAW"] == "Debug test message"
    assert message["PRIORITY"] == "7"
    assert message["CODE_FUNC"] == "log_writer"
    assert int(message["PID"]) == os.getpid()
    for field in REQUIRED_FIELDS:
        assert field in message


async def test_exception_message(log, fake_service) -> None:
    def log_writer() -> None:
        try:
            1 / 0
        except ZeroDivisionError:
            log.exception("Sample exception")

    message = await emit(fake_service, log_writer)
    assert message["MESSAGE"].startswith("Sample exception\nTraceback")
    assert message["MESSAGE_RAW"] == "Sample exception"
    assert message["PRIORITY"] == "3"
    assert message["CODE_FUNC"] == "log_writer"
    assert int(message["PID"]) == os.getpid()
    assert message["EXCEPTION_TYPE"] == "<class 'ZeroDivisionError'>"
    assert message["EXCEPTION_VALUE"] == "division by zero"
    assert message["TRACEBACK"].startswith("Traceback (most recent call last)")
    for field in REQUIRED_FIELDS:
        assert field in message


async def test_message_id_disabled(sock_path, fake_service) -> None:
    logger = logging.getLogger("test-no-message-id")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.handlers.append(JournaldLogHandler(socket_path=sock_path, use_message_id=False))

    def log_writer() -> None:
        logger.info("No message id")

    message = await emit(fake_service, log_writer)
    assert message["MESSAGE"] == "No message id"
    assert "MESSAGE_ID" not in message


def test_emit_falls_back_to_stderr_on_transport_error(sock_path, sock, capsys) -> None:
    handler = JournaldLogHandler(socket_path=sock_path)
    handler.transport.send = mock.Mock(side_effect=OSError("boom"))

    logger = logging.getLogger("test-fallback")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.handlers.append(handler)

    logger.info("Fallback message")

    captured = capsys.readouterr()
    assert "Unable to write message" in captured.err
    assert "Fallback message" in captured.err
