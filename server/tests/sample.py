"""A valid agent report (the shape agent/report.py builds), for tests to modify."""

import copy
from typing import Any

SAMPLE_REPORT: dict[str, Any] = {
    "hostname": "nas",
    "agent_version": "0.1.0",
    "timestamp": "2026-10-12T12:00:00Z",
    "updates": {"total": 0, "security": 0},
    "reboot_required": False,
    "containers": [
        {
            "name": "nginx",
            "state": "running",
            "status": "Up 2 hours (healthy)",
            "health": "healthy",
            "exit_code": None,
        }
    ],
    "failed_units": [],
    "timers": [
        {
            "timer": "apt-daily.timer",
            "activates": "apt-daily.service",
            "last_run": "Sun 2026-10-11 19:42:10 UTC",
            "next_run": "Mon 2026-10-12 06:12:00 UTC",
        }
    ],
    "disks": [{"mount": "/", "percent": 42}],
    "errors": [],
}


def make_report(**changes: Any) -> dict[str, Any]:
    """A deep copy of SAMPLE_REPORT with top-level keys replaced."""
    report = copy.deepcopy(SAMPLE_REPORT)
    report.update(changes)
    return report


CLUSTER_UID = "08528ee9-428d-5167-8a8e-df19e635932f"

# The `kubernetes` key of a report (agent/kube_cluster.py): a healthy two-node cluster.
SAMPLE_KUBERNETES: dict[str, Any] = {
    "node": {
        "role": "server",
        "dist": "k3s",
        "name": "nas",
        "k3s": "v1.31.2+k3s1",
        "kubelet": None,
        "helm": "v3.16.2",
    },
    "cluster": {
        "uid": CLUSTER_UID,
        "version": "v1.31.2+k3s1",
        "api": {"error": None, "cert_days": 290},
        "sa": "system:serviceaccount:monitoring:cronrack-agent",
        "token_days": 300,
        "denied": [],
        "missing": [],
        "nodes": [
            {
                "name": "nas",
                "roles": ["control-plane", "master"],
                "ready": True,
                "pressure": [],
                "kubelet": "v1.31.2+k3s1",
                "os": "linux",
                "arch": "amd64",
                "cpu_m": 4000,
                "mem_mi": 15800,
                "cpu_used_m": 420,
                "mem_used_mi": 3100,
            },
            {
                "name": "worker-1",
                "roles": [],
                "ready": True,
                "pressure": [],
                "kubelet": "v1.31.2+k3s1",
                "os": "linux",
                "arch": "arm64",
                "cpu_m": 4000,
                "mem_mi": 7800,
                "cpu_used_m": None,
                "mem_used_mi": None,
            },
        ],
        "problem_pods": [],
        "ok_pods": {"apps": [["web-5d9f7c8b6-abcde", "worker-1", 0, 86400]]},
        "pod_counts": {
            "total": 1,
            "running": 1,
            "pending": 0,
            "succeeded": 0,
            "failed": 0,
            "problem": 0,
        },
        "workloads": [
            {
                "kind": "Deployment",
                "ns": "apps",
                "name": "web",
                "desired": 1,
                "ready": 1,
                "available": 1,
                "updated": 1,
                "misscheduled": 0,
                "images": ["ghcr.io/example/web:1.4.2"],
                "helm": None,
            }
        ],
        "helm": [],
        "argo": [],
        "argocd_version": None,
        "pvcs": [],
        "events": [],
        "truncated": False,
        "cut": [],
        "incomplete": [],
    },
}


def make_kube_report(cluster: dict[str, Any] | None = ..., **node: Any) -> dict[str, Any]:
    """A valid report with a `kubernetes` key. `cluster=None` is a node without the cluster view;
    keyword arguments change the node part."""
    kube = copy.deepcopy(SAMPLE_KUBERNETES)
    kube["node"].update(node)
    if cluster is not ...:
        kube["cluster"] = cluster
    return make_report(agent_version="0.2.0", kubernetes=kube)


def make_cluster(**changes: Any) -> dict[str, Any]:
    """A deep copy of the sample cluster view with top-level keys replaced."""
    cluster = copy.deepcopy(SAMPLE_KUBERNETES["cluster"])
    cluster.update(changes)
    return cluster


GIB = 1024**3

# The `metrics` key of a report (agent/metrics.py, agent 0.4.0): a calm host with two
# mounts and two containers, one of them with a memory limit.
SAMPLE_METRICS: dict[str, Any] = {
    "interval_s": 60,
    "cpu": {
        "busy_pct": 23.4,
        "iowait_pct": 1.2,
        "load": [0.42, 0.38, 0.3],
        "cores": 4,
        "temp_c": 52.1,
    },
    "memory": {
        "total": 8 * GIB,
        "available": 5 * GIB,
        "swap_total": 2 * GIB,
        "swap_used": GIB // 4,
    },
    "mounts": [
        {
            "mount": "/",
            "fs": "ext4",
            "size": 120 * GIB,
            "free": 46 * GIB,
            "used_pct": 61.6,
            "inodes_pct": 12.4,
        },
        {
            "mount": "/srv/media",
            "fs": "btrfs",
            "size": 3800 * GIB,
            "free": 1900 * GIB,
            "used_pct": 50.0,
            "inodes_pct": None,
        },
    ],
    "disk_io": {"read_bps": 184320, "write_bps": 921600},
    "containers": [
        {
            "name": "jellyfin",
            "cpu_pct": 4.1,
            "mem": GIB,
            "mem_limit": None,
            "rx_bps": 10342,
            "tx_bps": 2214,
        },
        {
            "name": "immich-server",
            "cpu_pct": 1.5,
            "mem": GIB,
            "mem_limit": 2 * GIB,
            "rx_bps": 500,
            "tx_bps": 250,
        },
    ],
    "containers_truncated": False,
}


def make_metrics(**changes: Any) -> dict[str, Any]:
    """A deep copy of SAMPLE_METRICS with top-level keys replaced."""
    data = copy.deepcopy(SAMPLE_METRICS)
    data.update(changes)
    return data


def make_metrics_report(metrics: Any = ..., **changes: Any) -> dict[str, Any]:
    """A valid 0.4.0 report with a `metrics` key (SAMPLE_METRICS unless given)."""
    return make_report(
        agent_version="0.4.0", metrics=make_metrics() if metrics is ... else metrics, **changes
    )
