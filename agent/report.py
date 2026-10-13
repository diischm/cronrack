"""Build the report the agent sends: one JSON-ready dict per check-in.

The server's ReportSerializer (Phase 5) mirrors this format exactly. Keys:

    hostname, agent_version, timestamp,
    updates, reboot_required, containers, failed_units, timers, disks,
    errors

A check is null when it is disabled, its tool is missing, or it crashed.
Only a crash adds an entry to errors.
"""

import socket
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from agent import AGENT_VERSION
from agent import collectors as agent_collectors
from agent.config import AgentConfig

Collector = Callable[[], Any]

# Config check name -> key in the report, in report order.
REPORT_KEYS: dict[str, str] = {
    "updates": "updates",
    "reboot": "reboot_required",
    "containers": "containers",
    "units": "failed_units",
    "timers": "timers",
    "disks": "disks",
}


def command_check(run: Callable[[], str | None], parse: Callable[[str], Any]) -> Collector:
    """Join a run_*/parse_* pair into one collector. A missing tool (None) stays None."""

    def collect() -> Any:
        raw = run()
        return None if raw is None else parse(raw)

    return collect


DEFAULT_COLLECTORS: dict[str, Collector] = {
    "updates": command_check(
        agent_collectors.run_apt_upgradable, agent_collectors.parse_apt_upgradable
    ),
    "reboot": agent_collectors.check_reboot_required,
    "containers": command_check(agent_collectors.run_docker_ps, agent_collectors.parse_docker_ps),
    "units": command_check(
        agent_collectors.run_systemctl_failed, agent_collectors.parse_systemctl_failed
    ),
    "timers": command_check(
        agent_collectors.run_systemctl_timers, agent_collectors.parse_systemctl_timers
    ),
    "disks": command_check(agent_collectors.run_df, agent_collectors.parse_df),
}


def safe_collect(name: str, fn: Collector) -> tuple[Any, dict[str, str] | None]:
    """Run one collector. Returns (result, None), or (None, error entry) if it raised.

    The error entry holds only the exception's class name: messages and stack
    traces can contain file paths or other host details the server doesn't need.
    """
    try:
        return fn(), None
    # Deliberately broad: one broken check must never break the whole report.
    except Exception as exc:  # noqa: BLE001
        return None, {"check": name, "error": type(exc).__name__}


def format_timestamp(now: datetime) -> str:
    """UTC, ISO 8601, whole seconds, 'Z' suffix: 2026-10-12T12:00:00Z."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_report(
    config: AgentConfig,
    now: datetime | None = None,
    collectors: Mapping[str, Collector] | None = None,
) -> dict[str, Any]:
    """Run every enabled check and return the full report dict.

    `collectors` defaults to the real ones; tests pass fakes so no command runs.
    """
    if now is None:
        now = datetime.now(UTC)
    if collectors is None:
        collectors = DEFAULT_COLLECTORS

    report: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "agent_version": AGENT_VERSION,
        "timestamp": format_timestamp(now),
    }
    errors: list[dict[str, str]] = []
    for check, key in REPORT_KEYS.items():
        if check not in config.enabled_checks:
            report[key] = None
            continue
        result, error = safe_collect(check, collectors[check])
        report[key] = result
        if error is not None:
            errors.append(error)
    report["errors"] = errors
    return report
