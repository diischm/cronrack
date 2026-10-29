"""What happens to a cluster when its agent reports, and when the agent goes quiet.

The agent on one node reads the cluster through the Kubernetes API and sends the result
inside its normal report (report["kubernetes"]["cluster"]). The consumer splits the
report in two: the host keeps its own part (plus a tiny stub), and the bulk lands here,
in Cluster.latest_snapshot. A cluster belongs to the owner of the host whose agent
reports it. Every lookup uses (host.owner, uid), so another user's host with the same
node names, or even the same cluster, can never touch it.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from clusters.models import Cluster
from clusters.updates import latest_versions
from hosts.humanize import duration
from hosts.models import Host
from hosts.services import REPORT_INTERVAL_SECONDS, STUCK_AFTER_INTERVALS
from rules.base import Finding, fingerprint
from rules.engine import (
    CLUSTER_OFFLINE_RULE,
    OFFLINE,
    WAITING,
    apply_suppressions,
    compute_status,
    evaluate_cluster,
    load_cluster_overrides,
    load_cluster_suppressions,
    top_issues,
)
from rules.registry import get_rule

# Silence longer than this (or the owner's "mark offline after", if larger) means offline.
CLUSTER_SILENT_SECONDS = 180
# Another host's agent may take over a cluster only after this long without a snapshot.
STALE_AFTER_SECONDS = STUCK_AFTER_INTERVALS * REPORT_INTERVAL_SECONDS

NAME_MAX = Cluster._meta.get_field("name").max_length


def split_report(report: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """(the report the host keeps, the cluster view or None).

    The host keeps its node role and a stub ({"uid", "truncated"}) in place of the
    cluster view, so host pages and host rules don't carry data they never use.
    """
    kube = report.get("kubernetes")
    if not isinstance(kube, dict):
        return report, None
    cluster = kube.get("cluster")
    stub = None
    if isinstance(cluster, dict):
        stub = {"uid": cluster.get("uid"), "truncated": cluster.get("truncated") is True}
    host_part = {**report, "kubernetes": {"node": kube.get("node"), "cluster": stub}}
    return host_part, cluster if isinstance(cluster, dict) else None


def node_key(name: Any) -> str:
    """How a host name and a node name are compared: first DNS label, lowercased."""
    return name.lower().split(".")[0] if isinstance(name, str) else ""


def _default_name(owner_id: int, host: Host) -> str:
    base = f"{host.name}-cluster"[: NAME_MAX - 4]
    taken = set(Cluster.objects.filter(owner_id=owner_id).values_list("name", flat=True))
    name, number = base, 1
    while name in taken:
        number += 1
        name = f"{base}-{number}"
    return name


@dataclass(frozen=True)
class ClusterResult:
    cluster: Cluster
    old_status: str
    new_status: str
    findings: list[Finding]


def _active_findings(cluster, snapshot, now, overrides, suppressions, latest):
    findings = evaluate_cluster(cluster, snapshot, now, overrides=overrides, latest=latest)
    active, _ = apply_suppressions(
        findings, cluster, now, suppressions=suppressions, scope="cluster"
    )
    return active


def _find_or_create(host: Host, uid: str) -> Cluster:
    owner_id = host.owner_id
    cluster = Cluster.objects.select_for_update().filter(owner_id=owner_id, uid=uid).first()
    if cluster is not None:
        return cluster
    try:
        with transaction.atomic():
            return Cluster.objects.create(
                owner_id=owner_id, uid=uid, name=_default_name(owner_id, host)
            )
    except IntegrityError:
        # A second report created it first (two agents): use theirs.
        return Cluster.objects.select_for_update().get(owner_id=owner_id, uid=uid)


def _may_report(cluster: Cluster, host: Host, now: datetime) -> bool:
    """Only one host reports a cluster. A newcomer waits until the reporter went stale."""
    if cluster.reporter_id in (None, host.pk):
        return True
    received = cluster.snapshot_received_at
    return received is None or (now - received).total_seconds() > STALE_AFTER_SECONDS


def link_nodes(cluster: Cluster, snapshot: dict[str, Any]) -> None:
    """Set Host.cluster on the owner's hosts that are nodes of this cluster.

    A host is a node when its host name (first label, lowercased) equals a node name.
    Only the cluster owner's hosts are considered. Hosts that were linked but are no
    longer in the node list are unlinked. Nothing is guessed: a node name that matches no
    host stays unlinked.
    """
    names = {node_key(n["name"]) for n in snapshot.get("nodes") or []}
    matching = [
        pk
        for pk, report in Host.objects.filter(owner_id=cluster.owner_id).values_list(
            "pk", "latest_report"
        )
        if isinstance(report, dict) and node_key(report.get("hostname")) in names
    ]
    owned = Host.objects.filter(owner_id=cluster.owner_id)
    owned.filter(pk__in=matching).exclude(cluster=cluster).update(cluster=cluster)
    owned.filter(cluster=cluster).exclude(pk__in=matching).update(cluster=None)


def save_cluster_snapshot(
    host: Host, snapshot: dict[str, Any] | None, now: datetime | None = None
) -> ClusterResult | None:
    """Store the cluster view that `host`'s agent sent, evaluate it, record a status change.

    `snapshot` must have passed ClusterViewSerializer; `host` must come from the agent's
    token. Returns None when nothing was stored: no cluster view, an unknown cluster ID
    (the agent couldn't read kube-system) without an existing cluster of this host, or
    another host is still reporting this cluster.
    """
    if snapshot is None:
        return None
    if now is None:
        now = timezone.now()
    # Imported here: activity.events -> alerts -> prefs would otherwise load too early.
    from activity.events import record_status_change

    with transaction.atomic():
        uid = snapshot.get("uid")
        if uid is None:
            # Without kube-system's UID we can't tell which cluster this is. If this host
            # already reports one, the news (no permission, API down) is about that one.
            cluster = (
                Cluster.objects.select_for_update()
                .filter(owner_id=host.owner_id, reporter_id=host.pk)
                .first()
            )
            if cluster is None:
                return None
        else:
            cluster = _find_or_create(host, uid)
            if not _may_report(cluster, host, now):
                return None

        overrides = load_cluster_overrides(cluster)
        suppressions = load_cluster_suppressions(cluster)
        latest = latest_versions(cluster.owner_id)
        old_status = cluster.status
        cluster.latest_snapshot = snapshot
        cluster.snapshot_received_at = now
        cluster.last_seen_at = now
        cluster.reporter = host
        active = _active_findings(cluster, snapshot, now, overrides, suppressions, latest)
        cluster.status = compute_status(active, cluster, now)
        cluster.save(
            update_fields=[
                "latest_snapshot",
                "snapshot_received_at",
                "last_seen_at",
                "reporter",
                "status",
            ]
        )
        link_nodes(cluster, snapshot)
        if old_status != cluster.status:
            record_status_change(cluster, old_status, cluster.status, top_issues(active))
    return ClusterResult(cluster, old_status, cluster.status, active)


# --- dead-man's switch for clusters (run by the check_hosts scheduler) -------------------------


def silent_threshold(silent_after_seconds: int) -> int:
    return max(CLUSTER_SILENT_SECONDS, silent_after_seconds)


def cluster_silence_reason(
    cluster: Cluster, now: datetime, silent_after_seconds: int
) -> str | None:
    """Why this cluster counts as offline right now, or None. Never reported = waiting."""
    if cluster.last_seen_at is None or cluster.latest_snapshot is None:
        return None
    silent = (now - cluster.last_seen_at).total_seconds()
    if silent > silent_threshold(silent_after_seconds):
        return f"No cluster data for {duration(silent)}"
    return None


def effective_cluster_status(cluster: Cluster, now: datetime, silent_after_seconds: int) -> str:
    """The status to show right now, even when the scheduler isn't running."""
    if cluster.latest_snapshot is None:
        return WAITING
    if cluster_silence_reason(cluster, now, silent_after_seconds):
        return OFFLINE
    return cluster.status


def _offline_finding(cluster: Cluster, text: str) -> Finding:
    rule = get_rule(CLUSTER_OFFLINE_RULE)
    return Finding(
        rule_id=rule.id,
        subject="",
        severity=rule.default_severity,
        text=text,
        why=rule.why,
        fix=rule.fix_template,
        fingerprint=fingerprint(rule.id, f"c{cluster.pk}", ""),
    )


def mark_silent_clusters_offline(now: datetime | None = None) -> list[Cluster]:
    """Mark every cluster that stopped reporting as offline; record and alert each change.

    Clusters already offline or still waiting are skipped, so each outage produces
    exactly one event and one alert. The next snapshot brings the cluster back.
    """
    from activity.events import record_status_change
    from prefs.models import Prefs

    if now is None:
        now = timezone.now()
    default_silent = Prefs._meta.get_field("silent_after_seconds").default
    silent_after = dict(Prefs.objects.values_list("owner_id", "silent_after_seconds"))

    candidates = Cluster.objects.exclude(status__in=[Host.Status.WAITING, Host.Status.OFFLINE])
    marked = []
    for cluster_id, owner_id in candidates.values_list("pk", "owner_id"):
        threshold = silent_after.get(owner_id, default_silent)
        with transaction.atomic():
            # Re-read under a lock: a snapshot may have arrived since the query above.
            cluster = Cluster.objects.select_for_update().filter(pk=cluster_id).first()
            if cluster is None or cluster.status in (Host.Status.OFFLINE, Host.Status.WAITING):
                continue
            reason = cluster_silence_reason(cluster, now, threshold)
            if reason is None:
                continue
            old_status = cluster.status
            cluster.status = Host.Status.OFFLINE
            cluster.save(update_fields=["status"])
            record_status_change(
                cluster, old_status, cluster.status, [_offline_finding(cluster, reason)]
            )
        marked.append(cluster)
    return marked
