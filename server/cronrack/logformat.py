"""Log output: plain text or JSON lines on stdout, and quiet health probes.

settings.LOGGING wires these up. Everything goes to stdout, where `docker compose logs`
and `kubectl logs` read it; nothing is written to a file.

Logging is configured before Django loads its apps, so this module imports nothing
from Django or CronRack.
"""

import json
import logging
from datetime import UTC, datetime

# The paths cronrack/health.py answers (tests/test_logging.py checks they match).
PROBE_PATHS = frozenset({"/healthz", "/readyz"})


class JsonFormatter(logging.Formatter):
    """One JSON object per line (LOG_FORMAT=json), for log collectors.

    json.dumps escapes newlines and control characters, so text from a request (a path,
    an exception message) can never start a fake log line.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry)


class SkipProbeSuccess(logging.Filter):
    """Drop uvicorn's access-log lines for /healthz and /readyz that succeeded (2xx).

    Probes call them every few seconds: tens of thousands of identical lines a day.
    A failed probe (any other status) is still logged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access logs: '%s - "%s %s HTTP/%s" %d' with
        # (client, method, path with query, http version, status).
        args = record.args
        if not (isinstance(args, tuple) and len(args) == 5):
            return True
        path, status = args[2], args[4]
        if not isinstance(path, str) or not isinstance(status, int):
            return True
        return not (path.split("?", 1)[0] in PROBE_PATHS and 200 <= status < 300)
