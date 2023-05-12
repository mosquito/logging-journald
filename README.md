logging-journald
================

[![PyPI - License](https://img.shields.io/pypi/l/logging-journald)](https://pypi.org/project/logging-journald) [![Wheel](https://img.shields.io/pypi/wheel/logging-journald)](https://pypi.org/project/logging-journald) [![PyPI](https://img.shields.io/pypi/v/logging-journald)](https://pypi.org/project/logging-journald) [![PyPI](https://img.shields.io/pypi/pyversions/logging-journald)](https://pypi.org/project/logging-journald) [![Coverage Status](https://coveralls.io/repos/github/mosquito/logging-journald/badge.svg?branch=master)](https://coveralls.io/github/mosquito/logging-journald?branch=master) ![tests](https://github.com/mosquito/logging-journald/workflows/tests/badge.svg?branch=master)

Pure python logging handler for writing logs to the journald using
native protocol.

```python
import logging
from logging_journald import JournaldLogHandler, check_journal_stream

# Use python default handler
LOG_HANDLERS = None


if (
    # Check if program running as systemd service
    check_journal_stream() or
    # Check if journald socket is available
    JournaldLogHandler.SOCKET_PATH.exists()
):
    LOG_HANDLERS = [JournaldLogHandler()]

logging.basicConfig(level=logging.INFO, handlers=LOG_HANDLERS)
logging.info("Hello logging world.")
```
