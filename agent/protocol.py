"""Messages between the agent and the server (backend guide section 6).

parse_server_message() is the agent's main security gate. The server may send
exactly two things:

    {"type": "welcome", "interval_seconds": 60, "enabled_checks": ["updates", ...]}
    {"type": "collect"}

Anything else (unknown type, extra field, wrong type, value out of range, not
JSON at all) is dropped: the function returns None and logs only the message
type, never the payload. Nothing received is ever executed: a message can only
change how often the agent reports and ask for a report now.
"""

import json
import logging
import platform
import re
import socket
from typing import Any

from agent import AGENT_VERSION
from agent.config import MAX_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS, AgentConfig

logger = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 64 * 1024
# The server closes the socket on a message over 262,144 bytes (uvicorn --ws-max-size).
# Reports are kept below this, leaving room for the {"type": "report", ...} wrapper.
MAX_REPORT_BYTES = 250_000
MAX_CHECKS = 32

# Shape of a type or check name we're willing to even mention in a log line.
_NAME = re.compile(r"[a-z_]{1,32}")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

OS_RELEASE_PATH = "/etc/os-release"


def _clean(text: str, limit: int) -> str:
    """Single line, at most `limit` characters, never empty (the server requires that)."""
    text = _CONTROL_CHARS.sub(" ", text).strip()[:limit]
    return text or "unknown"


def os_name(path: str = OS_RELEASE_PATH) -> str:
    """'Ubuntu 24.04.1 LTS' from /etc/os-release, falling back to e.g. 'Linux'."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key == "PRETTY_NAME":
                    return _clean(value.strip().strip("\"'"), 100)
    except OSError:
        pass
    return _clean(platform.system(), 100)


def hello_message(config: AgentConfig) -> dict[str, Any]:
    # config is accepted for symmetry and future fields; the token is never part of a message.
    return {
        "type": "hello",
        "agent_version": AGENT_VERSION,
        "hostname": _clean(socket.gethostname(), 253),
        "os": os_name(),
    }


def report_message(report: dict[str, Any]) -> dict[str, Any]:
    return {"type": "report", "report": report}


def _log_rejected(reason: str, message_type: Any = None) -> None:
    shown = message_type if isinstance(message_type, str) and _NAME.fullmatch(message_type) else "?"
    logger.warning("Ignored a server message (type %s): %s", shown, reason)


def _is_int(value: Any) -> bool:
    # bool is a subclass of int, so `true` would otherwise count as 1.
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_welcome(data: dict[str, Any]) -> dict[str, Any] | None:
    if set(data) != {"type", "interval_seconds", "enabled_checks"}:
        _log_rejected("unexpected or missing fields", "welcome")
        return None
    interval, checks = data["interval_seconds"], data["enabled_checks"]
    if not _is_int(interval) or not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS:
        _log_rejected("bad interval_seconds", "welcome")
        return None
    if (
        not isinstance(checks, list)
        or len(checks) > MAX_CHECKS
        or not all(isinstance(c, str) and _NAME.fullmatch(c) for c in checks)
    ):
        _log_rejected("bad enabled_checks", "welcome")
        return None
    return {"type": "welcome", "interval_seconds": interval, "enabled_checks": list(checks)}


def parse_server_message(raw: Any) -> dict[str, Any] | None:
    """Return the message as a clean dict if it is a valid welcome or collect, else None."""
    if not isinstance(raw, str):
        _log_rejected("not a text frame")
        return None
    if len(raw.encode("utf-8", errors="replace")) > MAX_MESSAGE_BYTES:
        _log_rejected("too large")
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        _log_rejected("not JSON")
        return None
    if not isinstance(data, dict):
        _log_rejected("not a JSON object")
        return None

    message_type = data.get("type")
    if message_type == "welcome":
        return _parse_welcome(data)
    if message_type == "collect":
        if set(data) != {"type"}:
            _log_rejected("unexpected fields", "collect")
            return None
        return {"type": "collect"}
    _log_rejected("unknown type", message_type)
    return None
