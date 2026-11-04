"""Logging for both workers.

On Railway every line is one JSON object on stdout, so the log explorer shows
each line's real severity (plain stderr lines were all shown as "error") and
@logger:... filters work. Locally the plain text format is kept.
"""

import json
import logging
import os
import sys

_LEVELS = {"DEBUG": "debug", "INFO": "info", "WARNING": "warn", "ERROR": "error", "CRITICAL": "error"}


class RailwayJsonFormatter(logging.Formatter):
    def __init__(self, service):
        super().__init__()
        self.service = service

    def format(self, record):
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return json.dumps(
            {
                "level": _LEVELS.get(record.levelname, "info"),
                "message": message,
                "logger": record.name,
                "service": self.service,
            },
            ensure_ascii=False,
        )


def configure_logging(service):
    handler = logging.StreamHandler(sys.stdout)
    if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("LOG_FORMAT") == "json":
        handler.setFormatter(RailwayJsonFormatter(service))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
