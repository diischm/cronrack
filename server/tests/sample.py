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
