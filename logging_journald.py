import array
import errno
import fcntl
import logging
import os
import socket
import struct
import sys
import tempfile
import traceback
import uuid
from collections.abc import Iterable
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


class JournaldTransport:
    VALUE_LEN_STRUCT = struct.Struct("@Q")
    SOCKET_PATH = Path("/run/systemd/journal/socket")

    # Sending failed because the socket we hold is no longer the one at the path, so a
    # fresh connection is worth trying.
    RECONNECT_ERRNOS = frozenset({errno.ECONNREFUSED, errno.ENOTCONN, errno.EPIPE})

    # Whether to connect the socket once, or address the path on every send the way
    # libsystemd's sd_journal_sendv() does. Connecting is a little cheaper per message
    # and is the default because it is what this has always done. Addressing per send
    # survives the socket at that path being replaced, which is what a socket-activated
    # journald does every time it has been idle -- see send().
    CONNECTED = True

    def __init__(
        self,
        socket_path: str | Path | None = None,
        connected: bool | None = None,
    ):
        # Resolved here rather than as a default argument value: a default is bound
        # when the class is defined, so overriding SOCKET_PATH on the class (or in a
        # subclass) would have no effect on it.
        self.socket_path = Path(socket_path) if socket_path is not None else self.SOCKET_PATH
        self.connected = self.CONNECTED if connected is None else connected
        self.socket = self._connect() if self.connected else self._open()

    # This method is private because it is an internal helper of __init__/send, not public API
    def _open(self) -> socket.socket:
        return socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

    # This method is private because it is an internal helper of __init__/send, not public API
    def _connect(self) -> socket.socket:
        sock = self._open()
        sock.connect(str(self.socket_path))
        return sock

    # F_ADD_SEALS is exposed by the fcntl module only when it was defined in the
    # Linux headers Python was built against; some Python builds (observed with
    # python-build-standalone on GitHub Actions' ubuntu-latest, for several
    # versions) have memfd_create but not the sealing constants, so both are
    # checked rather than assuming one implies the other.
    if hasattr(os, "memfd_create") and hasattr(fcntl, "F_ADD_SEALS"):

        @staticmethod
        def memfd_open(*args: Any, **kwargs: Any) -> IO[bytes]:
            """Return memfd file-like object"""
            # memfd_create never touches the filesystem, so the name is
            # just a debug label (visible in /proc/self/fd) and doesn't
            # need tempfile's path-uniqueness guarantees.
            fd: int = os.memfd_create(
                uuid.uuid4().hex,
                os.MFD_ALLOW_SEALING,
            )
            return os.fdopen(fd, *args, **kwargs)

        @staticmethod
        def memfd_seal(fp: IO[bytes]) -> None:
            fp.flush()
            fcntl.fcntl(
                fp.fileno(),
                fcntl.F_ADD_SEALS,
                fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE | fcntl.F_SEAL_SEAL,
            )
    else:

        @staticmethod
        def memfd_open(*args: Any, **kwargs: Any) -> IO[bytes]:
            """Return python temporary file object"""
            return tempfile.TemporaryFile(*args, **kwargs)

        @staticmethod
        def memfd_seal(fp: IO[bytes]) -> None:
            pass

    @staticmethod
    def _encode_short(key: str, value: Any) -> bytes:
        return f"{key.upper()}={value}\n".encode()

    @classmethod
    def _encode_long(cls, key: str, value: bytes) -> bytes:
        length = cls.VALUE_LEN_STRUCT.pack(len(value))
        return key.upper().encode() + b"\n" + length + value + b"\n"

    @classmethod
    def pack(cls, fp: IO[bytes], key: str, value: Any) -> None:
        if value is None:
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
                cls.pack(fp, f"{key}_{idx}", item)
            return
        elif isinstance(value, dict):
            for d_key, d_value in value.items():
                cls.pack(fp, f"{key}_{d_key}", d_value)
            return

        cls.pack(fp, key, str(value).encode())
        return

    def send(self, pairs: Iterable[tuple[str, Any]]) -> None:
        with BytesIO() as fp:
            for key, value in pairs:
                self.pack(fp, key, value)
            value = fp.getvalue()

        try:
            self._send(value)
        except OSError as e:
            if not self.connected or e.errno not in self.RECONNECT_ERRNOS:
                raise
            # journald's socket unit can be restarted underneath us, which replaces the
            # socket file: a socket connected to the old one stays dead, and every send
            # after that fails. Reconnect and try once more. Unconnected there is
            # nothing to repair -- the path is resolved by each send already -- so the
            # error is the caller's to deal with.
            self.socket.close()
            self.socket = self._connect()
            self._send(value)

    def _send(self, value: bytes) -> None:
        try:
            if self.connected:
                self.socket.sendall(value)
            else:
                self.socket.sendto(value, str(self.socket_path))
        except OSError as e:
            if e.errno != errno.EMSGSIZE:
                # Anything else -- journald not listening, socket replaced, permission
                # denied -- is not something a file descriptor fixes, and going down
                # that path only replaces the error with one from the fallback.
                raise
            # the systemd standard way to handle long payloads
            with self.memfd_open("wb+") as mfp:
                # copy content to memfd
                mfp.write(value)

                self.memfd_seal(mfp)
                # sendmsg's data buffer must carry at least one (possibly empty)
                # element -- an actually empty list raises EMSGSIZE on some
                # platforms (observed on macOS) even though the ancillary data
                # is what matters here.
                ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [mfp.fileno()]))]
                if self.connected:
                    self.socket.sendmsg([b""], ancillary)
                else:
                    self.socket.sendmsg([b""], ancillary, 0, str(self.socket_path))


def check_journal_stream() -> bool:
    """Returns True if journald is listening on stderr otherwise False"""
    journal_stream = os.getenv("JOURNAL_STREAM", "")

    if not journal_stream:
        return False

    st_dev, st_ino = map(int, journal_stream.split(":", 1))
    stat = os.stat(sys.stderr.fileno())

    if stat.st_ino == st_ino and stat.st_dev == st_dev:
        return True

    return False


class JournaldLogHandler(logging.Handler):
    LEVELS = MappingProxyType(
        {
            logging.CRITICAL: 2,
            logging.DEBUG: 7,
            logging.FATAL: 0,
            logging.ERROR: 3,
            logging.INFO: 6,
            logging.NOTSET: 16,
            logging.WARNING: 4,
        }
    )

    RECORD_FIELDS_MAP = MappingProxyType(
        {
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
        }
    )

    __slots__ = ("_facility", "socket", "_identifier")

    SOCKET_PATH = JournaldTransport.SOCKET_PATH

    def __init__(
        self,
        identifier: str | None = None,
        facility: int = Facility.LOCAL7,
        use_message_id: bool = True,
        socket_path: str | Path | None = None,
    ):
        super().__init__()
        # As in JournaldTransport: resolved here so that overriding SOCKET_PATH on
        # this class keeps working.
        self.transport = JournaldTransport(
            socket_path=socket_path if socket_path is not None else self.SOCKET_PATH,
        )
        self._identifier = identifier
        self._facility = int(facility)
        self.use_message_id = use_message_id

    @staticmethod
    def _to_usec(ts: float) -> int:
        return int(ts * 1000000)

    def _format_record(self, record: logging.LogRecord) -> list[tuple[str, Any]]:
        message = self.format(record)
        message_traceback = ""
        message_level = self.LEVELS[record.levelno]
        message_facility = self._facility
        message_identifier = self._identifier
        message_code_string = f"{record.module}.{record.funcName}:{record.lineno}"

        result = [
            ("message", message),
            ("priority", message_level),
            ("syslog_facility", message_facility),
            ("syslog_identifier", message_identifier),
            ("code", message_code_string),
            ("code", dict(func=record.funcName, file=record.pathname, line=record.lineno, module=record.module)),
            ("created_usec", self._to_usec(record.created)),
            ("relative_usec", self._to_usec(record.relativeCreated)),
        ]

        message_id = None

        if record.exc_info:
            exc_type, exc_value, exc_tb = record.exc_info
            message_traceback = "\n".join(traceback.format_exception(*record.exc_info))
            result.append(("exception", dict(type=exc_type, value=exc_value)))
            result.append(("traceback", message_traceback))

        if self.use_message_id:
            message_hash = "\0".join(
                map(
                    str,
                    (
                        message,
                        traceback,
                        message_level,
                        message_facility,
                        message_identifier,
                        message_code_string,
                    ),
                ),
            )
            message_id = uuid.uuid3(uuid.NAMESPACE_OID, message_hash).hex
            result.append(("message_id", message_id))

        source = dict(record.__dict__)
        for field, name in self.RECORD_FIELDS_MAP.items():
            value = source.pop(field, None)
            if name is None or value is None:
                continue
            result.append((name, value))

        result.append(("extra", source))
        return result

    def _fallback(self, record: logging.LogRecord) -> None:
        sys.stderr.write("Unable to write message ")
        sys.stderr.write(repr(self.format(record)))
        sys.stderr.write(" to journald\n")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.transport.send(self._format_record(record))
        except Exception:
            self._fallback(record)


__all__ = (
    "Facility",
    "JournaldLogHandler",
    "JournaldTransport",
    "check_journal_stream",
)
