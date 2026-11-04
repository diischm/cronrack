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
from hosts.presenters import STATUS_LABELS, OwnerData, finding_summary, iso_utc, issue_dict
from rules.base import Finding
from rules.engine import (
    CLUSTER_OFFLINE_RULE,
    apply_suppressions,
    evaluate_cluster,
    top_issues,
)
from rules.k8s import pod_severity, workload_severity
from rules.versions import is_behind, minor_of

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
    status = effective_cluster_status(cluster, now, data.prefs.silent_after_seconds, data.overrides)
    return {"status": status, "status_label": STATUS_LABELS.get(status, status)}


def _snapshot(cluster: Cluster) -> dict[str, Any]:
    return cluster.latest_snapshot if isinstance(cluster.latest_snapshot, dict) else {}


def cluster_summary(cluster: Cluster, now: datetime, data: OwnerData) -> dict[str, Any]:
    """One cluster card on the overview and the clusters list."""
    snapshot = _snapshot(cluster)
    nodes = snapshot.get("nodes") or []
    counts = snapshot.get("pod_counts") or {}
    status = cluster_status(cluster, now, data)
    findings = active_cluster_findings(cluster, now, data)
    # Every active issue, like hosts: Legacy shows the first 3, Glass's expand region all.
    issues = top_issues(findings, n=len(findings))
    return {
        "id": cluster.pk,
        "name": cluster.name,
        **status,
        "summary": finding_summary(status["status"], issues),
        "issues": [{"text": f.text, "severity": f.severity} for f in issues],
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


def _row(name: str, installed: str | None, latest: str | None) -> dict:
    """One line of "Versions & updates". state: update | minor | current | unchecked.

    "minor" (a newer k3s minor is stable: plan an upgrade, info) is set by the k3s stable
    row only; every other row is "update" (yellow) when a newer release exists.
    """
    if installed is None or latest is None or minor_of(latest) is None:
        state = "unchecked"
    elif is_behind(installed, latest):
        state = "update"
    else:
        state = "current"
    return {"name": name, "installed": installed, "latest": latest, "state": state}


def _version_rows(snapshot: dict[str, Any], data: Any, helm: str | None) -> list[dict[str, Any]]:
    """k3s (its own line, and the stable channel), or Kubernetes without k3s; Argo CD; Helm.

    Without update checks every row is "unchecked" and only the installed version shows.
    Kubernetes without k3s is never compared: its release feed would need a redirect.
    """
    online = data.prefs.check_updates_online
    latest = data.latest if online else {}
    channels = latest.get("k3s") or {}
    version = snapshot.get("version")
    rows = []
    minor = minor_of(version)
    if version and "+k3s" in version and minor is not None:
        line = f"v{minor[0]}.{minor[1]}"
        rows.append(_row(f"k3s ({line} line)", version, channels.get(line)))
        # The stable channel only says whether a newer minor exists; patches are the line's job.
        stable = channels.get("stable")
        stable_minor = minor_of(stable)
        if stable_minor is None:
            state = "unchecked"
        else:
            state = "minor" if stable_minor > minor else "current"
        rows.append(
            {"name": "k3s stable channel", "installed": version, "latest": stable, "state": state}
        )
    elif version:
        rows.append(_row("Kubernetes", version, None))
    argocd = snapshot.get("argocd_version")
    if argocd:
        rows.append(_row("Argo CD", argocd, (latest.get("argocd") or {}).get("latest")))
    if helm:
        rows.append(
            _row("Helm CLI (reporting host)", helm, (latest.get("helm") or {}).get("latest"))
        )
    return rows


def versions_context(snapshot: dict[str, Any], data: Any, helm: str | None) -> dict[str, Any]:
    """Installed versions, and the newest ones when the owner turned update checks on.

    `data` needs .prefs.check_updates_online and .latest (an OwnerData, or sample data).
    """
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
        "rows": _version_rows(snapshot, data, helm),
    }


def _reporter_helm(cluster: Cluster) -> str | None:
    """The Helm CLI version on the host that reports the cluster (Helm has no server side)."""
    reporter = cluster.reporter
    if reporter is None or reporter.owner_id != cluster.owner_id:
        return None
    report = reporter.latest_report if isinstance(reporter.latest_report, dict) else {}
    kube = report.get("kubernetes")
    node = kube.get("node") if isinstance(kube, dict) else None
    return node.get("helm") if isinstance(node, dict) else None


def snapshot_sections(
    snapshot: dict[str, Any],
    findings: list[Finding],
    linked_hosts: dict[str, dict[str, Any]],
    versions: dict[str, Any],
) -> dict[str, Any]:
    """Everything on the cluster page that comes from the snapshot and the findings.

    No database access: cluster_context() passes the real data, the dev preview
    (clusters.samples) fixed sample data, so both always have the same shape.
    linked_hosts maps node_key(node name) to {"id", "name", "url"} of a CronRack host.
    """
    nodes, unlinked = [], []
    for node in snapshot.get("nodes") or []:
        host = linked_hosts.get(node_key(node["name"]))
        nodes.append({**node, "host": host})
        if host is None:
            unlinked.append(node["name"])

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
    # Each row carries its colour (the rules' view), so templates only print values.
    workloads = [
        {
            **w,
            "severity": workload_severity(w),
            "available_text": f"{w['available']}/{w['desired']}",
        }
        for w in snapshot.get("workloads") or []
    ]
    counts = snapshot.get("pod_counts") or {}
    argo = snapshot.get("argo") or []
    return {
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
            "argo": len(argo),
            "argo_synced": sum(1 for a in argo if a["sync"] == "Synced"),
            "pvcs": len(snapshot.get("pvcs") or []),
            # Releases to install (yellow rows); None while update checks are off.
            "updates": sum(1 for r in versions["rows"] if r["state"] == "update")
            if versions["updates_online"]
            else None,
        },
        "nodes": nodes,
        "unlinked_nodes": unlinked,
        "problem_pods": [
            {**p, "severity": pod_severity(p)} for p in aged(snapshot.get("problem_pods") or [])
        ],
        "ok_pods": ok_pods,
        "workloads": workloads,
        "helm": snapshot.get("helm") or [],
        "argo": argo,
        "pvcs": aged(snapshot.get("pvcs") or []),
        "events": aged(snapshot.get("events") or []),
        "versions": versions,
        "api": snapshot.get("api"),
        "token_days": snapshot.get("token_days"),
        "denied": snapshot.get("denied") or [],
        "missing": snapshot.get("missing") or [],
        "truncated": snapshot.get("truncated") is True,
        "cut": snapshot.get("cut") or [],
        "incomplete": snapshot.get("incomplete") or [],
    }


def cluster_context(cluster: Cluster, now: datetime) -> dict[str, Any]:
    """Everything the cluster page and /api/v1/clusters/<id>/detail.json show."""
    data = OwnerData(cluster.owner)
    snapshot = _snapshot(cluster)
    linked = {
        key: {"id": host.pk, "name": host.name, "url": reverse("host-detail", args=[host.pk])}
        for key, host in _node_hosts(cluster).items()
    }
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
        **snapshot_sections(
            snapshot,
            active_cluster_findings(cluster, now, data),
            linked,
            versions_context(snapshot, data, _reporter_helm(cluster)),
        ),
    }
