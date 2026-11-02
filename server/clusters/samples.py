"""Fixed sample clusters for the dev-only preview (/dev/preview/cluster/, cronrack.dev_views).

Nothing here comes from the database or an agent. The findings are the real cluster rules
run over a sample snapshot, and the page sections come from the same snapshot_sections()
as the real page, so the preview always looks like what a user would see.

States: healthy, broken (every kind of problem at once, update checks on), xss (hostile
text in every name), truncated (lists cut and not read in full), offline, waiting.
"""

import copy
from types import SimpleNamespace
from typing import Any

from django.utils import timezone

from clusters.presenters import snapshot_sections, versions_context
from hosts.presenters import STATUS_LABELS
from rules.engine import CLUSTER_OFFLINE_RULE, evaluate_cluster

STATES = ("healthy", "broken", "xss", "truncated", "offline", "waiting")

# The newest releases as the daily update check would store them (broken state only).
LATEST = {
    "k3s": {"stable": "v1.33.1+k3s1", "v1.31": "v1.31.9+k3s1", "v1.33": "v1.33.1+k3s1"},
    "argocd": {"latest": "v3.5.6"},
    "helm": {"latest": "v3.19.0"},
}

# Matches the overview preview (hosts.samples): homeserver is the server, pi-01 the worker.
LINKED_HOSTS = {
    "server-1": {"id": 3, "name": "homeserver", "url": "/hosts/3/"},
    "worker-1": {"id": 1, "name": "pi-01", "url": "/hosts/1/"},
}


def _node(name: str, roles: list[str], arch: str, **changes: Any) -> dict[str, Any]:
    node = {
        "name": name,
        "roles": roles,
        "ready": True,
        "pressure": [],
        "kubelet": "v1.31.2+k3s1",
        "os": "linux",
        "arch": arch,
        "cpu_m": 4000,
        "mem_mi": 15800 if arch == "amd64" else 7800,
        "cpu_used_m": 420 if arch == "amd64" else 180,
        "mem_used_mi": 3100 if arch == "amd64" else 1900,
    }
    node.update(changes)
    return node


def _workload(kind: str, ns: str, name: str, desired: int, available: int, image: str) -> dict:
    return {
        "kind": kind,
        "ns": ns,
        "name": name,
        "desired": desired,
        "ready": available,
        "available": available,
        "updated": desired,
        "misscheduled": 0,
        "images": [image],
        "helm": None,
    }


def _pod(name: str, phase: str, reason: str | None, age_s: int, **changes: Any) -> dict:
    pod = {
        "ns": "apps",
        "name": name,
        "node": "server-1",
        "phase": phase,
        "ready": [0, 1],
        "restarts": 0,
        "reason": reason,
        "last": None,
        "last_age_s": None,
        "age_s": age_s,
        "owner": "Deployment/" + name.split("-")[0],
    }
    pod.update(changes)
    return pod


def _event(obj: str, reason: str, msg: str, count: int, age_s: int) -> dict:
    return {"ns": "apps", "obj": obj, "reason": reason, "msg": msg, "count": count, "age_s": age_s}


HEALTHY: dict[str, Any] = {
    "uid": "11111111-2222-3333-4444-555555555555",
    "version": "v1.31.2+k3s1",
    "api": {"error": None, "cert_days": 290},
    "sa": "system:serviceaccount:monitoring:cronrack-agent",
    "token_days": 300,  # nosec B105 - days left, not a password
    "denied": [],
    "missing": [],
    "nodes": [
        _node("server-1", ["control-plane", "master"], "amd64"),
        _node("worker-1", [], "arm64"),
    ],
    "problem_pods": [],
    "ok_pods": {
        "apps": [["web-5d9f7c8b6-abcde", "worker-1", 0, 86400], ["db-0", "server-1", 0, 86400]],
        "argocd": [["argocd-server-7f45b5f545-b6fq8", "server-1", 0, 259200]],
        "kube-system": [
            ["coredns-8db54c48d-rpzmg", "server-1", 0, 259200],
            ["metrics-server-786d997795-6sx6g", "server-1", 1, 259200],
            ["traefik-9bcdbbd9-4znnj", "server-1", 0, 259200],
        ],
    },
    "pod_counts": {
        "total": 6,
        "running": 6,
        "pending": 0,
        "succeeded": 0,
        "failed": 0,
        "problem": 0,
    },
    "workloads": [
        _workload("Deployment", "apps", "web", 2, 2, "ghcr.io/example/web:1.4.2"),
        _workload("StatefulSet", "apps", "db", 1, 1, "docker.io/library/postgres:16.4"),
        _workload("Deployment", "argocd", "argocd-server", 1, 1, "quay.io/argoproj/argocd:v2.13.1"),
        _workload(
            "Deployment", "kube-system", "coredns", 1, 1, "docker.io/rancher/coredns-coredns:1.11.3"
        ),
        _workload(
            "DaemonSet", "kube-system", "svclb-traefik", 2, 2, "docker.io/rancher/klipper-lb:v0.4.9"
        ),
    ],
    "helm": [
        {
            "name": "traefik",
            "ns": "kube-system",
            "chart": "traefik-27.0.201+up27.0.2",
            "app_version": None,
            "source": "helmchart",
        },
        {
            "name": "web",
            "ns": "apps",
            "chart": "web-1.4.2",
            "app_version": "1.4.2",
            "source": "labels",
        },
    ],
    "argo": [
        {
            "name": "web",
            "ns": "argocd",
            "project": "default",
            "sync": "Synced",
            "health": "Healthy",
            "rev": "a1b2c3d",
            "op": "Succeeded",
            "synced_at": "2026-10-28T10:00:00Z",
        }
    ],
    "argocd_version": "v2.13.1",
    "pvcs": [
        {
            "ns": "apps",
            "name": "data-db-0",
            "phase": "Bound",
            "class": "local-path",
            "size": "1Gi",
            "age_s": 86400,
        }
    ],
    "events": [],
    "truncated": False,
    "cut": [],
    "incomplete": [],
}


def _broken() -> dict[str, Any]:
    snap = copy.deepcopy(HEALTHY)
    snap["api"]["cert_days"] = 20
    snap["nodes"][0]["pressure"] = ["MemoryPressure"]
    snap["nodes"][1].update(ready=False, cpu_used_m=None, mem_used_mi=None)
    snap["problem_pods"] = [
        _pod(
            "web-5d9f7c8b6-x2k4q",
            "Running",
            "CrashLoopBackOff",
            86400,
            restarts=12,
            last="OOMKilled",
            last_age_s=120,
        ),
        _pod("api-6c9d8f7b5-q8w2e", "Pending", "ImagePullBackOff", 900),
        _pod("batch-7d4f9c6b8-z1x2c", "Pending", "Unschedulable", 1800, node=None),
    ]
    snap["pod_counts"] = {
        "total": 9,
        "running": 6,
        "pending": 2,
        "succeeded": 0,
        "failed": 0,
        "problem": 3,
    }
    snap["workloads"][0].update(available=0, ready=0)
    snap["workloads"].insert(
        1, _workload("Deployment", "apps", "api", 1, 0, "ghcr.io/example/api:latest")
    )
    snap["argo"][0].update(sync="OutOfSync", health="Degraded", op="Failed")
    snap["pvcs"].append(
        {
            "ns": "apps",
            "name": "uploads",
            "phase": "Pending",
            "class": "local-path",
            "size": "5Gi",
            "age_s": 1200,
        }
    )
    snap["denied"] = [{"g": "metrics.k8s.io", "r": "nodes", "v": "list"}]
    snap["events"] = [
        _event(
            "Pod/web-5d9f7c8b6-x2k4q",
            "BackOff",
            "Back-off restarting failed container web in pod web-5d9f7c8b6-x2k4q",
            12,
            300,
        ),
        _event(
            "Pod/api-6c9d8f7b5-q8w2e",
            "Failed",
            'Failed to pull image "ghcr.io/example/api:latest": not found',
            4,
            360,
        ),
        _event(
            "Pod/batch-7d4f9c6b8-z1x2c",
            "FailedScheduling",
            "0/2 nodes are available: 1 Insufficient memory, 1 node(s) were not ready.",
            9,
            420,
        ),
    ]
    return snap


def _xss() -> dict[str, Any]:
    # Hostile text everywhere a name or message goes; the page must show all of it as text.
    # Imported here: cronrack.dev_views imports this module.
    from cronrack.dev_views import XSS_IMG, XSS_SCRIPT

    snap = _broken()
    snap["problem_pods"][0].update(name=XSS_SCRIPT, ns=XSS_IMG)
    snap["ok_pods"] = {XSS_IMG: [[XSS_SCRIPT, XSS_IMG, 0, 5]]}
    snap["workloads"][0].update(name=XSS_SCRIPT, ns=XSS_IMG)
    snap["helm"][0].update(name=XSS_IMG, chart=XSS_SCRIPT)
    snap["argo"][0].update(name=XSS_SCRIPT, project=XSS_IMG)
    snap["pvcs"][0].update(name=XSS_IMG)
    snap["events"][0].update(obj=XSS_IMG, msg=XSS_SCRIPT)
    return snap


def _truncated() -> dict[str, Any]:
    snap = _broken()
    snap.update(truncated=True, cut=["ok_pods", "events"], incomplete=["pods"])
    return snap


SNAPSHOTS = {
    "healthy": lambda: copy.deepcopy(HEALTHY),
    "broken": _broken,
    "xss": _xss,
    "truncated": _truncated,
    "offline": lambda: copy.deepcopy(HEALTHY),
    "waiting": dict,
}


def _status(findings: list) -> str:
    severities = {f.severity for f in findings}
    if "red" in severities:
        return "red"
    return "yellow" if "yellow" in severities else "green"


def preview_context(state: str = "healthy") -> dict[str, Any]:
    """The cluster page's context (clusters.presenters.cluster_context shape) for one state."""
    if state not in STATES:
        state = "healthy"
    now = timezone.now()
    snapshot = SNAPSHOTS[state]()
    online = state in ("broken", "truncated")
    latest = LATEST if online else {}
    fake = SimpleNamespace(pk=1, owner_id=0, last_seen_at=now, latest_snapshot=snapshot)
    findings = [
        f
        for f in evaluate_cluster(fake, snapshot, now, overrides=[], latest=latest)
        if f.rule_id != CLUSTER_OFFLINE_RULE
    ]
    if state in ("offline", "waiting"):
        status, findings = state, []
    else:
        status = _status(findings)
    data = SimpleNamespace(prefs=SimpleNamespace(check_updates_online=online), latest=latest)

    name = "homelab"
    if state == "xss":
        from cronrack.dev_views import XSS_SCRIPT

        name = XSS_SCRIPT
    waiting = state == "waiting"
    return {
        "cluster": {
            "id": 1,
            "name": name,
            "status": status,
            "status_label": STATUS_LABELS.get(status, status),
            "has_snapshot": not waiting,
            "last_seen_human": "2 h ago" if state == "offline" else "25s ago",
            "snapshot_received_at": None if waiting else "2026-10-29T12:00:00Z",
            "reporter": None if waiting else "homeserver",
            "version": snapshot.get("version"),
            "url": "/clusters/1/",
            "settings_url": "/clusters/1/settings/",
            # Empty on purpose: cluster.js then doesn't refresh the preview from real data.
            "json_url": "",
        },
        **snapshot_sections(
            snapshot,
            findings,
            {} if waiting else LINKED_HOSTS,
            versions_context(snapshot, data, None if waiting else "v3.16.2"),
        ),
    }
