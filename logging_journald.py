import array
import fcntl
import logging
import os
import socket
import struct
import sys
import tempfile
import traceback
import uuid
from enum import IntEnum, unique
from io import BytesIO
from pathlib import Path
from types import MappingProxyType
from typing import IO, Any


@unique
class Facility(IntEnum):
    KERN = 0
    USER = 1
    MAIL = 2
    DAEMON = 3
    AUTH = 4
    SYSLOG = 5
    LPR = 6
    NEWS = 7
    UUCP = 8
    CLOCK_DAEMON = 9
    AUTHPRIV = 10
    FTP = 11
    NTP = 12
    AUDIT = 13
    ALERT = 14
    CRON = 15
    LOCAL0 = 16
    LOCAL1 = 17
    LOCAL2 = 18
    LOCAL3 = 19
    LOCAL4 = 20
    LOCAL5 = 21
    LOCAL6 = 22
    LOCAL7 = 23


def check_journal_stream() -> bool:
    """ Returns True if journald is listening on stderr otherwise False """
    journal_stream = os.getenv("JOURNAL_STREAM", "")

    if not journal_stream:
        return False

    st_dev, st_ino = map(int, journal_stream.split(":", 1))
    stat = os.stat(sys.stderr.fileno())

    if stat.st_ino == st_ino and stat.st_dev == st_dev:
        return True

    return False


class JournaldLogHandler(logging.Handler):
    LEVELS = MappingProxyType({
        logging.CRITICAL: 2,
        logging.DEBUG: 7,
        logging.FATAL: 0,
        logging.ERROR: 3,
        logging.INFO: 6,
        logging.NOTSET: 16,
        logging.WARNING: 4,
    })

    VALUE_LEN_STRUCT = struct.Struct("@Q")
    SOCKET_PATH = Path("/run/systemd/journal/socket")

    RECORD_FIELDS_MAP = MappingProxyType({
        "args": "arguments",
        "created": None,
        "exc_info": None,
        "exc_text": None,
        "filename": None,
        "funcName": None,
        "levelname": None,
        "levelno": None,
        "lineno": None,
        "message": None,
        "module": None,
        "msecs": None,
        "msg": "message_raw",
        "name": "logger_name",
        "pathname": None,
        "process": "pid",
        "processName": "process_name",
        "relativeCreated": None,
        "thread": "thread_id",
        "threadName": "thread_name",
    })

    JOURNAL_STREAM = check_journal_stream()

    __slots__ = ("__facility", "socket", "__identifier")

    if hasattr(os, "memfd_create"):
        @staticmethod
        def memfd_open(*args, **kwargs) -> IO[bytes]:
            """ Return memfd file-like object """
            fd = os.memfd_create(tempfile.mktemp(), os.MFD_ALLOW_SEALING)
            return os.fdopen(fd, *args, **kwargs)

        @staticmethod
        def memfd_seal(fp: IO[bytes]) -> None:
            fp.flush()
            fcntl.fcntl(
                fp.fileno(),
                fcntl.F_ADD_SEALS,
                fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW |
                fcntl.F_SEAL_WRITE | fcntl.F_SEAL_SEAL,
            )
    else:
        @staticmethod
        def memfd_open(*args, **kwargs) -> IO[bytes]:
            """ Return python temporary file object """
            return tempfile.TemporaryFile(*args, **kwargs)

        @staticmethod
        def memfd_seal(fp: IO[bytes]) -> None:
            pass

    @staticmethod
    def _encode_short(key: str, value: Any) -> bytes:
        return "{}={}\n".format(key.upper(), value).encode()

    @classmethod
    def _encode_long(cls, key: str, value: bytes) -> bytes:
        length = cls.VALUE_LEN_STRUCT.pack(len(value))
        return key.upper().encode() + b"\n" + length + value + b"\n"

    @classmethod
    def pack(cls, fp: IO[bytes], key: str, value: Any) -> None:
        if not value:
            return
        elif isinstance(value, (int, float)):
            fp.write(cls._encode_short(key, value))
            return
        elif isinstance(value, str):
            if "\n" in value:
                fp.write(cls._encode_long(key, value.encode()))
                return
            fp.write(cls._encode_short(key, value))
            return
        elif isinstance(value, bytes):
            fp.write(cls._encode_long(key, value))
            return
        elif isinstance(value, (list, tuple)):
            for idx, item in enumerate(value):
                cls.pack(fp, "{}_{}".format(key, idx), item)
            return
        elif isinstance(value, dict):
            for d_key, d_value in value.items():
                cls.pack(fp, "{}_{}".format(key, d_key), d_value)
            return

        cls.pack(fp, key, str(value).encode())
        return

    def __init__(
        self, identifier: str = None,
        facility: int = Facility.LOCAL7,
    ):
        super().__init__()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.connect(str(self.SOCKET_PATH))
        self.__identifier = identifier
        self.__facility = int(facility)

    @staticmethod
    def _to_usec(ts: float) -> int:
        return int(ts * 1000000)

    def __format_record(
        self, fp: IO[bytes], record: logging.LogRecord,
    ) -> None:
        message_id = uuid.uuid1().hex

        self.pack(fp, "message", self.format(record))
        self.pack(fp, "message_id", message_id)

        if record.exc_info:
            exc_type, exc_value, exc_tb = record.exc_info
            self.pack(
                fp, "exception", {
                    "type": exc_type,
                    "value": exc_value,
                },
            )
            tb_message = "\n".join(
                traceback.format_exception(*record.exc_info),
            )
            self.pack(fp, "traceback", tb_message)

        self.pack(fp, "priority", self.LEVELS[record.levelno])
        self.pack(fp, "syslog_facility", self.__facility)
        self.pack(fp, "syslog_identifier", self.__identifier)
        self.pack(
            fp, "code", "{}.{}:{}".format(
                record.module, record.funcName, record.lineno,
            ),
        )

        self.pack(
            fp, "code", {
                "func": record.funcName,
                "file": record.pathname,
                "line": record.lineno,
                "module": record.module,
            },
        )

        self.pack(fp, "created_usec", self._to_usec(record.created))
        self.pack(
            fp, "relative_usec", self._to_usec(record.relativeCreated),
        )

        source = dict(record.__dict__)

        for field, name in self.RECORD_FIELDS_MAP.items():
            value = source.pop(field, None)
            if name is None or value is None:
                continue
            self.pack(fp, name, value)

        self.pack(fp, "extra", source)

    def _fallback(self, record) -> None:
        sys.stderr.write("Unable to write message ")
        sys.stderr.write(repr(self.format(record)))
        sys.stderr.write(" to journald\n")

    def emit(self, record: logging.LogRecord) -> None:
        with BytesIO() as fp:
            # noinspection PyBroadException
            try:
                try:
                    self.__format_record(fp, record)
                    self.socket.sendall(fp.getvalue())
                except OSError:
                    # the systemd standard way to handle long payloads
                    with self.memfd_open("wb+") as mfp:
                        # copy content to memfd
                        mfp.write(fp.getvalue())

                        self.memfd_seal(mfp)
                        self.socket.sendmsg(
                            [],
                            [(
                                socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                array.array("i", [mfp.fileno()]),
                            )],
                        )
            except Exception:
                self._fallback(record)


__all__ = (
    "JournaldLogHandler",
    "check_journal_stream",
)
