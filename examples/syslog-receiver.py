import logging
import re
from abc import abstractmethod, ABC
from datetime import datetime
from typing import Any, Dict

from logging_journald import JournaldTransport

from aiomisc import entrypoint, threaded
from aiomisc.service.udp import UDPServer
from aiomisc.service.sdwatchdog import SDWatchdogService


__doc__ = """
This example demonstrates how to create an async syslog server that forwards messages to journald.
It also demonstrates how to create a NetConsole server that forwards messages to journald.
"""

class JournaldTransportService(UDPServer, ABC):
    transport: JournaldTransport


    @threaded
    def send_log(self, **kwargs):
        self.transport.send(kwargs.items())

    @abstractmethod
    async def parse_message(self, message) -> Dict[str, Any]:
        pass

    async def handle_datagram(self, data, addr):
        message = data.decode()
        parsed_message = {'message': message, 'hostname': addr[0]}
        parsed_message.update(await self.parse_message(message))
        await self.send_log(**parsed_message)

    async def start(self) -> None:
        self.transport = JournaldTransport()
        await super().start()


class SyslogService(JournaldTransportService):
    """
    Syslog is a standard for message logging.
    This is an implementation of a syslog server, which forwards messages to journald.
    """

    # We have a more different syslog message formats, but this is the most common one.
    SYSLOG_PATTERN = re.compile(
        r'^<(?P<syslog_priority>\d+)>(?P<syslog_timestamp>\w{3} +\d+ \d+:\d+:\d+) (?P<syslog_hostname>\S+) '
        r'((?P<syslog_applicaton>[^:]+): )?((?P<syslog_process>[^\[]+)\[(?P<syslog_pid>\d+)\]:\s+?)?(?P<message>.*)'
    )

    @staticmethod
    def parse_timestamp(timestamp):
        now = datetime.now()
        try:
            try:
                return datetime.strptime(timestamp, '%b %d %H:%M:%S').replace(year=now.year)
            except ValueError:
                return datetime.strptime(timestamp, '%b %d %Y %H:%M:%S')
        except ValueError:
            return now

    @threaded
    def parse_message(self, message):
        match = self.SYSLOG_PATTERN.match(message)
        result = {'syslog_raw': message, 'message': message}
        if match:
            try:
                parsed = match.groupdict()
                for key, value in parsed.items():
                    if value is not None:
                        result[key] = value

                timestamp = parsed['syslog_timestamp']

                # pri is: facility * 8 + severity, so we can get facility
                # and severity by dividing and getting the remainder.
                pri = int(parsed['syslog_priority'])
                result['priority'] = pri % 8
                result['timestamp'] = int(self.parse_timestamp(timestamp).timestamp())
                result['syslog_facility'] = pri // 8
            except Exception:
                logging.exception('Failed to parse syslog message')
        return result


class NetConsoleService(JournaldTransportService):
    """ NetConsole is a simple protocol that sends messages from the kernel to a remote host. """

    async def parse_message(self, message):
        """ Nothing to parse, just forward the message. """
        result = {'priority': 6, 'message': message}
        return result


def main():
    with entrypoint(
        # Register the Syslog service to listen on all interfaces and port 514.
        SyslogService(address="::", port=514),

        # Register the NetConsole service to listen on all interfaces and port 6666.
        NetConsoleService(address="::", port=6666),

        # Register the watchdog service to notify systemd that the service is running.
        # And answering the watchdog heartbeats.
        SDWatchdogService(),
        pool_size=16
    ) as loop:
        loop.run_forever()


if __name__ == '__main__':
    main()
