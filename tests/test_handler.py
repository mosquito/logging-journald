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
from typing import Any, Deque, Mapping, Tuple

import aiomisc
from aiomisc import bind_socket, threaded
from aiomisc.service import UDPServer
from pytest_subtests import SubTests

from logging_journald import Facility, JournaldLogHandler, JournaldTransport, check_journal_stream


def test_check_journal_stream() -> None:
    stat = os.stat(sys.stderr.fileno())
    os.environ["JOURNAL_STREAM"] = f"{stat.st_dev}:{stat.st_ino}"
    assert check_journal_stream()

    os.environ["JOURNAL_STREAM"] = ""
    assert not check_journal_stream()

    del os.environ["JOURNAL_STREAM"]
    assert not check_journal_stream()


def test_facility() -> None:
    assert Facility(0) == Facility.KERN
    assert Facility["KERN"] == Facility.KERN


def test_journald_logger(  # noqa: C901
    loop: asyncio.AbstractEventLoop, subtests: SubTests,
) -> None:
    with TemporaryDirectory(dir="/tmp") as tmp_dir:
        tmp_path = Path(tmp_dir)
        sock_path = tmp_path / "notify.sock"

        logs: Deque[Mapping[str, Any]] = deque()

        class FakeJournald(UDPServer):
            VALUE_LEN_STRUCT = struct.Struct("@Q")

            async def handle_datagram(
                self, data: bytes, addr: Tuple[Any, ...],
            ) -> None:
                result = {}
                with BytesIO(data) as fp:
                    line = fp.readline()
                    while line:
                        if b"=" not in line:
                            key = line.decode().strip()
                            value_len = self.VALUE_LEN_STRUCT.unpack(
                                fp.read(
                                    self.VALUE_LEN_STRUCT.size,
                                ),
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

                logs.append(result)

        @threaded
        def log_writer() -> None:
            log = logging.getLogger("test")
            log.propagate = False
            log.setLevel(logging.DEBUG)
            log.handlers.clear()
            log.handlers.append(JournaldLogHandler())

            log.info("Test message")
            log.info("Test multiline\nmessage")
            log.info(
                "Test formatted: int=%d str=%s repr=%r float=%0.1f",
                1, 2, 3, 4,
            )

            log.info(
                "Message with extra", extra={
                    "foo": "bar",
                },
            )

            log.warning("Warning test message")
            log.critical("Critical test message")
            log.error("Error test message")
            log.log(logging.FATAL, "Fatal test message")
            log.debug("Debug test message")

            try:
                1 / 0
            except ZeroDivisionError:
                log.exception("Sample exception")

        with bind_socket(
            socket.AF_UNIX, socket.SOCK_DGRAM, address=str(sock_path),
        ) as sock:
            JournaldTransport.SOCKET_PATH = sock_path

            with aiomisc.entrypoint(FakeJournald(sock=sock), loop=loop):
                loop.run_until_complete(log_writer())

    assert len(logs) == 10

    required_fields = {
        "MESSAGE", "MESSAGE_ID", "MESSAGE_RAW", "PRIORITY",
        "SYSLOG_FACILITY", "CODE", "CODE_FUNC", "CODE_FILE",
        "CODE_LINE", "CODE_MODULE", "LOGGER_NAME", "PID",
        "PROCESS_NAME", "THREAD_ID", "THREAD_NAME",
        "RELATIVE_USEC", "CREATED_USEC",
    }

    with subtests.test("simple message"):
        message = logs.popleft()
        assert message["MESSAGE"] == "Test message"
        assert message["MESSAGE_RAW"] == "Test message"
        assert message["PRIORITY"] == "6"
        assert message["CODE_FUNC"] == "log_writer"
        assert int(message["PID"]) == os.getpid()
        for field in required_fields:
            assert field in message

    with subtests.test("multiline message"):
        message = logs.popleft()
        assert message["MESSAGE"] == "Test multiline\nmessage"
        assert message["MESSAGE_RAW"] == "Test multiline\nmessage"
        assert message["PRIORITY"] == "6"
        assert message["CODE_FUNC"] == "log_writer"
        assert int(message["PID"]) == os.getpid()

        for field in required_fields:
            assert field in message

    with subtests.test("formatted message"):
        message = logs.popleft()
        assert message["MESSAGE"] == (
            "Test formatted: int=1 str=2 repr=3 float=4.0"
        )
        assert message["MESSAGE_RAW"] == (
            "Test formatted: int=%d str=%s repr=%r float=%0.1f"
        )
        assert message["ARGUMENTS_0"] == "1"
        assert message["ARGUMENTS_1"] == "2"
        assert message["ARGUMENTS_2"] == "3"
        assert message["ARGUMENTS_3"] == "4"
        assert message["PRIORITY"] == "6"
        assert message["CODE_FUNC"] == "log_writer"
        assert int(message["PID"]) == os.getpid()

        for field in required_fields:
            assert field in message

    with subtests.test("message with extra"):
        message = logs.popleft()
        assert message["MESSAGE"] == "Message with extra"
        assert message["MESSAGE_RAW"] == "Message with extra"
        assert message["PRIORITY"] == "6"
        assert message["CODE_FUNC"] == "log_writer"
        assert message["EXTRA_FOO"] == "bar"
        assert int(message["PID"]) == os.getpid()

        for field in required_fields:
            assert field in message

    with subtests.test("warning message"):
        message = logs.popleft()
        assert message["MESSAGE"] == "Warning test message"
        assert message["MESSAGE_RAW"] == "Warning test message"
        assert message["PRIORITY"] == "4"
        assert message["CODE_FUNC"] == "log_writer"
        assert int(message["PID"]) == os.getpid()

        for field in required_fields:
            assert field in message

    with subtests.test("critical message"):
        message = logs.popleft()
        assert message["MESSAGE"] == "Critical test message"
        assert message["MESSAGE_RAW"] == "Critical test message"
        assert message["PRIORITY"] == "0"
        assert message["CODE_FUNC"] == "log_writer"
        assert int(message["PID"]) == os.getpid()

        for field in required_fields:
            assert field in message

    with subtests.test("error message"):
        message = logs.popleft()
        assert message["MESSAGE"] == "Error test message"
        assert message["MESSAGE_RAW"] == "Error test message"
        assert message["PRIORITY"] == "3"
        assert message["CODE_FUNC"] == "log_writer"
        assert int(message["PID"]) == os.getpid()

        for field in required_fields:
            assert field in message

    with subtests.test("fatal message"):
        message = logs.popleft()
        assert message["MESSAGE"] == "Fatal test message"
        assert message["MESSAGE_RAW"] == "Fatal test message"
        assert message["PRIORITY"] == "0"
        assert message["CODE_FUNC"] == "log_writer"
        assert int(message["PID"]) == os.getpid()

        for field in required_fields:
            assert field in message

    with subtests.test("debug message"):
        message = logs.popleft()
        assert message["MESSAGE"] == "Debug test message"
        assert message["MESSAGE_RAW"] == "Debug test message"
        assert message["PRIORITY"] == "7"
        assert message["CODE_FUNC"] == "log_writer"
        assert int(message["PID"]) == os.getpid()

        for field in required_fields:
            assert field in message

    with subtests.test("exception message"):
        message = logs.popleft()
        assert message["MESSAGE"].startswith("Sample exception\nTraceback")
        assert message["MESSAGE_RAW"] == "Sample exception"
        assert message["PRIORITY"] == "3"
        assert message["CODE_FUNC"] == "log_writer"
        assert int(message["PID"]) == os.getpid()
        assert message["EXCEPTION_TYPE"] == "<class 'ZeroDivisionError'>"
        assert message["EXCEPTION_VALUE"] == "division by zero"
        assert message["TRACEBACK"].startswith(
            "Traceback (most recent call last)",
        )

        for field in required_fields:
            assert field in message
