logging-journald
================

Pure python logging handler for writing logs to the journald using
native protocol.

.. code-block:: python

    import logging
    from logging_journald import JournaldLogHandler

    # Use python default handler
    LOG_HANDLERS = None


    if (
        # Check if program running as systemd service
        JournaldLogHandler.JOURNAL_STREAM or
        # Check if journald socket is available
        JournaldLogHandler.SOCKET_PATH.exists()
    ):
        LOG_HANDLERS = [JournaldLogHandler()]

    logging.basicConfig(level=logging.INFO, handlers=LOG_HANDLERS)
    logging.info("Hello logging world.")
