"""Load and check the agent's local config file (/etc/cronrack/agent.toml).

The file will hold the host's token, so two rules apply everywhere here:
- the token never appears in error messages, logs, repr() or str();
- error messages name the bad *key*, never the value that was in it.
"""

import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

DEFAULT_CONFIG_PATH = "/etc/cronrack/agent.toml"
# B105 reviewed: this is the *name* of an environment variable, not a secret.
TOKEN_ENV_VAR = "CRONRACK_TOKEN"  # nosec B105

MVP_CHECKS = ("updates", "reboot", "containers", "units", "timers", "disks")

# Below 10s the agent would mostly be busy running commands; above a day the
# server's dead-man's switch would mark the host offline long before a report.
MIN_INTERVAL_SECONDS = 10
MAX_INTERVAL_SECONDS = 86_400

# Plain http:// / ws:// is only allowed to these hosts (local development, SSH tunnels).
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
WEBSOCKET_PATH = "/ws/agent/"

# Server tokens come from secrets.token_urlsafe(32): 43 URL-safe characters. Checking
# the shape also keeps anything odd (spaces, newlines) out of the HTTP header.
_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,128}")

PERMISSION_FIX = (
    "Run: sudo chown root:cronrack {path} && sudo chmod 640 {path} "
    "(installed service), or chmod 600 {path} for your own test file."
)


class ConfigError(Exception):
    """The config file is missing something, malformed, or unsafe."""


@dataclass
class AgentConfig:
    server_url: str | None = None
    token: str | None = None
    ca_file: str | None = None
    interval_seconds: int = 60
    enabled_checks: list[str] = field(default_factory=lambda: list(MVP_CHECKS))
    actions: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        # Written by hand so the token can't leak into logs or tracebacks.
        # str() falls back to this too.
        token = "<hidden>" if self.token else None
        return (
            f"AgentConfig(server_url={self.server_url!r}, token={token}, "
            f"ca_file={self.ca_file!r}, interval_seconds={self.interval_seconds!r}, "
            f"enabled_checks={self.enabled_checks!r}, actions={self.actions!r})"
        )


def _process_groups() -> set[int]:
    return {os.getgid(), *os.getgroups()}


def _check_stat(st: os.stat_result, path: str, uid: int, groups: set[int]) -> None:
    """Refuse a config file that anyone but us (and root) could read or change.

    Allowed: root:cronrack 0640 (the installed service: the agent can read its token
    but can't rewrite its own config), and <me>:<me> 0600 (a local test file).
    """
    mode = stat.S_IMODE(st.st_mode)
    fix = PERMISSION_FIX.format(path=path)
    if mode & stat.S_IRWXO:
        raise ConfigError(
            f"{path} is accessible by others (mode {mode:03o}); it holds the token. {fix}"
        )
    if mode & (stat.S_IWGRP | stat.S_IXGRP):
        raise ConfigError(f"{path} is writable or executable by its group (mode {mode:03o}). {fix}")
    if st.st_uid not in (0, uid):
        raise ConfigError(f"{path} is owned by another user (uid {st.st_uid}). {fix}")
    if mode & stat.S_IRGRP and st.st_gid not in groups:
        raise ConfigError(
            f"{path} is readable by group {st.st_gid}, which this agent isn't in. {fix}"
        )


def check_permissions(path: str) -> None:
    """Raise ConfigError if the file's owner or mode would expose or allow changing the token."""
    try:
        st = os.stat(path)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from None
    _check_stat(st, path, os.getuid(), _process_groups())


def check_server_url(url: str) -> str:
    """Validate the server's base URL and return it. Raises ConfigError.

    https:// (or wss://) is required. Plain http:// / ws:// is only accepted for
    localhost, 127.0.0.1 and ::1, so the token never crosses a network unencrypted.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        raise ConfigError("'server_url' is not a valid URL") from None
    if parts.scheme not in {"https", "wss", "http", "ws"}:
        raise ConfigError("'server_url' must start with https://")
    if not host:
        raise ConfigError("'server_url' has no host name")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("'server_url' must not contain a user name, password, ? or #")
    if parts.scheme in {"http", "ws"} and host not in LOCAL_HOSTS:
        raise ConfigError(
            "'server_url' must use https:// (plain http:// is only allowed for "
            "localhost, 127.0.0.1 and ::1)"
        )
    return url


def websocket_url(server_url: str) -> str:
    """The agent endpoint for a base URL: https://x -> wss://x/ws/agent/, http -> ws.

    A URL that already ends in /ws/agent/ is kept (older config files used that form).
    """
    parts = urlsplit(check_server_url(server_url))
    scheme = {"https": "wss", "http": "ws"}.get(parts.scheme, parts.scheme)
    path = parts.path.rstrip("/")
    if not path.endswith(WEBSOCKET_PATH.rstrip("/")):
        path += WEBSOCKET_PATH.rstrip("/")
    return f"{scheme}://{parts.netloc}{path}/"


def check_token(token: str) -> str:
    if not _TOKEN.fullmatch(token):
        # Never include the value: it may be a real (if mangled) secret.
        raise ConfigError(
            "'token' doesn't look like a CronRack token (copy it again from the server)"
        )
    return token


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"'{key}' must be a string")
    return value or None


def _parse(data: dict[str, Any]) -> AgentConfig:
    known = {"server_url", "token", "ca_file", "interval_seconds", "enabled_checks", "actions"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown setting(s): {', '.join(unknown)}")

    config = AgentConfig(
        server_url=_optional_str(data, "server_url"),
        token=_optional_str(data, "token"),
        ca_file=_optional_str(data, "ca_file"),
    )
    if config.server_url is not None:
        check_server_url(config.server_url)
    if config.token is not None:
        check_token(config.token)

    if "interval_seconds" in data:
        interval = data["interval_seconds"]
        # bool is a subclass of int in Python, so `true` would otherwise pass.
        if not isinstance(interval, int) or isinstance(interval, bool):
            raise ConfigError("'interval_seconds' must be a whole number")
        if not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS:
            raise ConfigError(
                f"'interval_seconds' must be between {MIN_INTERVAL_SECONDS} "
                f"and {MAX_INTERVAL_SECONDS}"
            )
        config.interval_seconds = interval

    if "enabled_checks" in data:
        checks = data["enabled_checks"]
        if not isinstance(checks, list) or not all(isinstance(c, str) for c in checks):
            raise ConfigError("'enabled_checks' must be a list of strings")
        unknown_checks = sorted(set(checks) - set(MVP_CHECKS))
        if unknown_checks:
            raise ConfigError(
                f"unknown check(s) in 'enabled_checks': {', '.join(unknown_checks)} "
                f"(known: {', '.join(MVP_CHECKS)})"
            )
        config.enabled_checks = list(dict.fromkeys(checks))  # drop duplicates, keep order

    if "actions" in data:
        if not isinstance(data["actions"], dict):
            raise ConfigError("'actions' must be a table")
        config.actions = data["actions"]

    return config


def load_config(path: str = DEFAULT_CONFIG_PATH) -> AgentConfig:
    """Read the TOML config. A missing file gives the defaults.

    The CRONRACK_TOKEN environment variable, if set, replaces the file's token.
    """
    try:
        with open(path, "rb") as f:
            # Check the file we actually opened, so it can't be swapped in between.
            _check_stat(os.fstat(f.fileno()), path, os.getuid(), _process_groups())
            data = tomllib.load(f)
    except FileNotFoundError:
        data = {}
    except tomllib.TOMLDecodeError as exc:
        # tomllib's message is a description plus line/column, never file content.
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from None

    config = _parse(data)

    env_token = os.environ.get(TOKEN_ENV_VAR)
    if env_token:
        config.token = check_token(env_token)
    return config
