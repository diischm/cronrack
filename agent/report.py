"""Build the report the agent sends: one JSON-ready dict per check-in.

The server's ReportSerializer (Phase 5) mirrors this format exactly. Keys:

    hostname, agent_version, timestamp,
    updates, reboot_required, containers, failed_units, timers, disks,
    errors

A check is null when it is disabled, its tool is missing, or it crashed.
Only a crash adds an entry to errors.

When [kubernetes] enabled is set, a `kubernetes` key ({"node": ..., "cluster": ...})
is added before errors. Without it the report is exactly as before.
"""

import json
import socket
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from functools import partial
from typing import Any

from agent import AGENT_VERSION
from agent import collectors as agent_collectors
from agent.config import AgentConfig, KubernetesConfig
from agent.kube_api import KubeClient
from agent.kube_cluster import SHRINK_STEPS, collect_cluster_view, shrink_cluster_view
from agent.protocol import MAX_REPORT_BYTES, _clean, report_message

Collector = Callable[[], Any]

KUBERNETES_KEY = "kubernetes"

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


def collect_kubernetes(kube: KubernetesConfig, now: datetime) -> dict[str, Any]:
    """This host's Kubernetes node role, plus the cluster view if it is switched on."""
    cluster = None
    if kube.cluster_view:
        client = KubeClient(kube.api_server, kube.token_file, kube.ca_file)
        cluster = collect_cluster_view(client, now)
    return {"node": agent_collectors.collect_node_role(), "cluster": cluster}


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
        # Same cleanup as the hello message: one line, at most 253 characters, never empty.
        "hostname": _clean(socket.gethostname(), 253),
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
    if config.kubernetes.enabled:
        collect = collectors.get(KUBERNETES_KEY) or partial(
            collect_kubernetes, config.kubernetes, now
        )
        result, error = safe_collect(KUBERNETES_KEY, collect)
        report[KUBERNETES_KEY] = result
        if error is not None:
            errors.append(error)
    report["errors"] = errors
    return report


def _message_size(report: dict[str, Any]) -> int:
    # Measured the way connection._send writes it, so the number is what goes on the wire.
    return len(json.dumps(report_message(report)).encode())


def fit_report(report: dict[str, Any], max_bytes: int = MAX_REPORT_BYTES) -> dict[str, Any]:
    """Make sure the report fits in one message, cutting Kubernetes data if it must.

    The server drops connections that send more than 256 KiB, and the agent would then
    reconnect and send the same thing again forever. The least important cluster data
    goes first (see kube_cluster.shrink_cluster_view); `truncated` and `cut` say what.
    The input is not modified. Host data (containers, units ...) is never cut here.
    """
    kube = report.get(KUBERNETES_KEY)
    if _message_size(report) <= max_bytes or not isinstance(kube, dict):
        return report
    view = kube.get("cluster")
    if not isinstance(view, dict):
        return report
    for step in range(1, SHRINK_STEPS + 1):
        shrunk = shrink_cluster_view(view, step)
        if shrunk is None:
            continue
        view = shrunk
        candidate = {**report, KUBERNETES_KEY: {**kube, "cluster": view}}
        if _message_size(candidate) <= max_bytes:
            return candidate
    # Even the bare identity doesn't fit (the host part alone is huge): drop the cluster.
    return {**report, KUBERNETES_KEY: {**kube, "cluster": None}}
