"""JSON log lines (``LOG_FORMAT=json``) for log shippers.

Fields: time, level, logger, message, plus the structured ``extra`` fields
the modules attach (``event``, ``topic``, ``event_id``, ...). Exceptions
are included as ``exc_info`` text. Callers never pass secrets or message
payloads in ``extra``.
"""

import json
import logging

# Attributes every LogRecord has; anything else came from ``extra``.
_STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and not key.startswith("_"):
                data[key] = value
        if record.exc_info:
            data["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)
