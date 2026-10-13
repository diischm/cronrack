"""Load and check the agent's local config file (/etc/cronrack/agent.toml).

The file will hold the host's token, so two rules apply everywhere here:
- the token never appears in error messages, logs, repr() or str();
- error messages name the bad *key*, never the value that was in it.
"""

import os
import stat
import tomllib
from dataclasses import dataclass, field
from typing import Any

DEFAULT_CONFIG_PATH = "/etc/cronrack/agent.toml"
# B105 reviewed: this is the *name* of an environment variable, not a secret.
TOKEN_ENV_VAR = "CRONRACK_TOKEN"  # nosec B105

MVP_CHECKS = ("updates", "reboot", "containers", "units", "timers", "disks")

# Below 10s the agent would mostly be busy running commands; above a day the
# server's dead-man's switch would mark the host offline long before a report.
MIN_INTERVAL_SECONDS = 10
MAX_INTERVAL_SECONDS = 86_400

# Group or others may not read or write the file.
_FORBIDDEN_MODE_BITS = stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH


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


def _check_mode(mode: int, path: str) -> None:
    if mode & _FORBIDDEN_MODE_BITS:
        raise ConfigError(
            f"{path} is readable or writable by group/others "
            f"(mode {stat.S_IMODE(mode):03o}); it holds the agent token. Run: chmod 600 {path}"
        )


def check_permissions(path: str) -> None:
    """Raise ConfigError if group or others can read or write the file."""
    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from None
    _check_mode(mode, path)


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
            _check_mode(os.fstat(f.fileno()).st_mode, path)
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
        config.token = env_token
    return config
