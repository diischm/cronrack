"""Turn a cluster and its snapshot into the plain dicts the cluster pages and JSON use.

The snapshot passed the strict serializers, but it is still agent data: every value is
only ever output with {{ }} (auto-escaped) or as JSON, never marked safe.
"""

from datetime import datetime
from typing import Any

from django.urls import reverse

from clusters.models import Cluster
from clusters.services import effective_cluster_status, node_key
from hosts.humanize import ago, duration
from hosts.models import Host
from hosts.presenters import STATUS_LABELS, OwnerData, iso_utc, issue_dict
from rules.base import Finding
from rules.engine import (
    CLUSTER_OFFLINE_RULE,
    apply_suppressions,
    evaluate_cluster,
    top_issues,
)

UPDATES_HINT = "Turn on update checks in Settings to compare with the latest releases"


def active_cluster_findings(cluster: Cluster, now: datetime, data: OwnerData) -> list[Finding]:
    """The cluster's current problems, after overrides and suppressions.

    Like hosts, the "stopped reporting" finding is not listed: the status says it.
    """
    if cluster.latest_snapshot is None:
        return []
    findings = evaluate_cluster(
        cluster, cluster.latest_snapshot, now, overrides=data.overrides, latest=data.latest
    )
    active, _ = apply_suppressions(
        findings, cluster, now, suppressions=data.suppressions, scope="cluster"
    )
    return [f for f in active if f.rule_id != CLUSTER_OFFLINE_RULE]


def cluster_status(cluster: Cluster, now: datetime, data: OwnerData) -> dict[str, str]:
    status = effective_cluster_status(cluster, now, data.prefs.silent_after_seconds)
    return {"status": status, "status_label": STATUS_LABELS.get(status, status)}


def _snapshot(cluster: Cluster) -> dict[str, Any]:
    return cluster.latest_snapshot if isinstance(cluster.latest_snapshot, dict) else {}


def cluster_summary(cluster: Cluster, now: datetime, data: OwnerData) -> dict[str, Any]:
    """One cluster card on the overview and the clusters list."""
    snapshot = _snapshot(cluster)
    nodes = snapshot.get("nodes") or []
    counts = snapshot.get("pod_counts") or {}
    return {
        "id": cluster.pk,
        "name": cluster.name,
        **cluster_status(cluster, now, data),
        "issues": [
            {"text": f.text, "severity": f.severity}
            for f in top_issues(active_cluster_findings(cluster, now, data))
        ],
        "last_seen_human": ago(cluster.last_seen_at, now),
        "url": reverse("cluster-detail", args=[cluster.pk]),
        "nodes": {"ready": sum(1 for n in nodes if n["ready"]), "total": len(nodes)},
        "problem_pods": counts.get("problem", 0),
        "version": snapshot.get("version"),
    }


def _node_hosts(cluster: Cluster) -> dict[str, Host]:
    """Linked hosts by node name. Only the cluster owner's hosts are ever listed."""
    hosts = {}
    for host in Host.objects.filter(cluster=cluster, owner_id=cluster.owner_id):
        report = host.latest_report if isinstance(host.latest_report, dict) else {}
        hosts[node_key(report.get("hostname"))] = host
    return hosts


def _versions(snapshot: dict[str, Any], data: OwnerData) -> dict[str, Any]:
    """Installed versions, and the newest ones when the owner turned update checks on."""
    online = data.prefs.check_updates_online
    channels = data.latest.get("k3s") or {}
    argocd = (data.latest.get("argocd") or {}).get("latest")
    nodes = [n for n in snapshot.get("nodes") or [] if "+k3s" in (n.get("kubelet") or "")]
    return {
        "updates_online": online,
        "hint": None if online else UPDATES_HINT,
        "kubernetes": snapshot.get("version"),
        "k3s_stable": channels.get("stable") if online else None,
        "k3s_nodes": [{"name": n["name"], "version": n["kubelet"]} for n in nodes],
        "argocd": snapshot.get("argocd_version"),
        "argocd_latest": argocd if online else None,
    }


def cluster_context(cluster: Cluster, now: datetime) -> dict[str, Any]:
    """Everything the cluster page and /api/v1/clusters/<id>/detail.json show."""
    data = OwnerData(cluster.owner)
    findings = active_cluster_findings(cluster, now, data)
    snapshot = _snapshot(cluster)
    hosts = _node_hosts(cluster)

    nodes, unlinked = [], []
    for node in snapshot.get("nodes") or []:
        host = hosts.get(node_key(node["name"]))
        entry = {**node, "host": None}
        if host is not None:
            entry["host"] = {
                "id": host.pk,
                "name": host.name,
                "url": reverse("host-detail", args=[host.pk]),
            }
        else:
            unlinked.append(node["name"])
        nodes.append(entry)

    def aged(items: list[dict], key: str = "age_s") -> list[dict]:
        return [
            {**i, "age_human": duration(i[key]) if i.get(key) is not None else "—"} for i in items
        ]

    ok_pods = [
        {
            "ns": ns,
            "pods": [
                {
                    "name": p[0],
                    "node": p[1],
                    "restarts": p[2],
                    "age_human": duration(p[3]) if p[3] is not None else "—",
                }
                for p in pods
            ],
        }
        for ns, pods in sorted((snapshot.get("ok_pods") or {}).items())
    ]
    workloads = snapshot.get("workloads") or []
    counts = snapshot.get("pod_counts") or {}
    return {
        "cluster": {
            "id": cluster.pk,
            "name": cluster.name,
            **cluster_status(cluster, now, data),
            "has_snapshot": cluster.latest_snapshot is not None,
            "last_seen_human": ago(cluster.last_seen_at, now),
            "snapshot_received_at": iso_utc(cluster.snapshot_received_at),
            "reporter": cluster.reporter.name if cluster.reporter else None,
            "version": snapshot.get("version"),
            "url": reverse("cluster-detail", args=[cluster.pk]),
            "settings_url": reverse("cluster-settings", args=[cluster.pk]),
            "json_url": reverse("cluster-detail-json", args=[cluster.pk]),
        },
        "issues": [issue_dict(f) for f in top_issues(findings, n=len(findings))],
        "tiles": {
            "nodes_ready": sum(1 for n in nodes if n["ready"]),
            "nodes_total": len(nodes),
            "pods": {
                k: counts.get(k, 0) for k in ("total", "running", "pending", "failed", "problem")
            },
            "workloads_total": len(workloads),
            "workloads_ok": sum(
                1 for w in workloads if w["available"] >= w["desired"] and not w["misscheduled"]
            ),
            "helm": len(snapshot.get("helm") or []),
            "argo": len(snapshot.get("argo") or []),
            "pvcs": len(snapshot.get("pvcs") or []),
        },
        "nodes": nodes,
        "unlinked_nodes": unlinked,
        "problem_pods": aged(snapshot.get("problem_pods") or []),
        "ok_pods": ok_pods,
        "workloads": workloads,
        "helm": snapshot.get("helm") or [],
        "argo": snapshot.get("argo") or [],
        "pvcs": aged(snapshot.get("pvcs") or []),
        "events": aged(snapshot.get("events") or []),
        "versions": _versions(snapshot, data),
        "api": snapshot.get("api"),
        "token_days": snapshot.get("token_days"),
        "denied": snapshot.get("denied") or [],
        "missing": snapshot.get("missing") or [],
        "truncated": snapshot.get("truncated") is True,
        "cut": snapshot.get("cut") or [],
    }
