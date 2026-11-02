"""What happens to a cluster when its agent reports, and when the agent goes quiet.

The agent on one node reads the cluster through the Kubernetes API and sends the result
inside its normal report (report["kubernetes"]["cluster"]). The consumer splits the
report in two: the host keeps its own part (plus a tiny stub), and the bulk lands here,
in Cluster.latest_snapshot. A cluster belongs to the owner of the host whose agent
reports it. Every lookup uses (host.owner, uid), so another user's host with the same
node names, or even the same cluster, can never touch it.
"""

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.contrib.auth import get_user_model
from django.core.cache import cache
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
    resolve_override,
    top_issues,
)
from rules.registry import get_rule

logger = logging.getLogger(__name__)

# A host is a node of one cluster at a time; 3 leaves room for a reinstall or two (each
# one has a new kube-system UID) before the owner deletes the old clusters. With the
# owner-wide cap, this stops a stolen agent token from creating clusters, and the first
# alert each new cluster sends, without end. Deleting a cluster frees a slot.
MAX_CLUSTERS_PER_HOST = 3
MAX_CLUSTERS_PER_OWNER = 20
CAP_LOG_SECONDS = 3600
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
    """ "<host>-cluster", or "<host>-cluster-2", -3 ... when taken. Always fits NAME_MAX: the
    base is shortened to make room for the suffix."""
    base = f"{host.name}-cluster"
    taken = set(Cluster.objects.filter(owner_id=owner_id).values_list("name", flat=True))
    name, number = base[:NAME_MAX], 1
    while name in taken:
        number += 1
        suffix = f"-{number}"
        name = base[: NAME_MAX - len(suffix)] + suffix
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


CREATE_ATTEMPTS = 3


def _cap_reached(host: Host) -> bool:
    """True if `host` may not add another cluster (its own cap, or its owner's).

    Logs a warning at most once per host per CAP_LOG_SECONDS. The "already logged" mark
    lives in Django's cache, which is LocMemCache (settings.CACHES): per process, and
    forgotten on restart, so a restart may log once more. Only IDs are logged.
    """
    owner_clusters = Cluster.objects.filter(owner_id=host.owner_id)
    host_count = owner_clusters.filter(reporter_id=host.pk).count()
    if host_count < MAX_CLUSTERS_PER_HOST and owner_clusters.count() < MAX_CLUSTERS_PER_OWNER:
        return False
    if cache.add(f"cluster-cap-logged:{host.pk}", True, CAP_LOG_SECONDS):
        logger.warning(
            "Host %s (owner %s): cluster limit reached, new cluster refused",
            host.pk,
            host.owner_id,
        )
    return True


def _find_or_create(host: Host, uid: str) -> Cluster | None:
    """The owner's cluster with this UID, a new one, or None when a cap says no."""
    owner_id = host.owner_id
    clusters = Cluster.objects.select_for_update().filter(owner_id=owner_id)
    cluster = clusters.filter(uid=uid).first()
    if cluster is not None:
        return cluster
    # A new cluster ID. Lock the owner's row first: every new cluster of this owner (from
    # any of its hosts) waits here in turn, so two reports can't both count below a cap.
    # (SQLite's IMMEDIATE transactions already run one writer at a time; the lock keeps
    # this true on a database that doesn't.)
    list(get_user_model().objects.select_for_update().filter(pk=owner_id).values_list("pk"))
    for _ in range(CREATE_ATTEMPTS):
        cluster = clusters.filter(uid=uid).first()
        if cluster is not None:
            return cluster
        if _cap_reached(host):
            return None
        try:
            with transaction.atomic():
                return Cluster.objects.create(
                    owner_id=owner_id, uid=uid, name=_default_name(owner_id, host)
                )
        except IntegrityError:
            # Another report won a race: it created this cluster (found on the next
            # round), or took the name we picked (the next round picks another one).
            continue
    raise IntegrityError("could not create the cluster")


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
    (the agent couldn't read kube-system) without an existing cluster of this host,
    another host is still reporting this cluster, or a new cluster would pass
    MAX_CLUSTERS_PER_HOST / MAX_CLUSTERS_PER_OWNER.
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
            if cluster is None or not _may_report(cluster, host, now):
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


def cluster_offline_threshold(
    cluster: Cluster, silent_after_seconds: int, overrides: Iterable[Any]
) -> float | None:
    """Seconds without a snapshot after which `cluster` is offline; None means never.

    The one place this is decided, for the pages and the scheduler alike:
    - k8s.cluster_offline disabled by an override: None (never offline, no finding);
    - an override sets a threshold: that number;
    - otherwise the rule's default_threshold, or the owner's "mark offline after"
      (Prefs.silent_after_seconds) if that is larger.
    `overrides` are the owner's overrides; ones for other owners, rules, hosts or
    clusters are skipped (rules.engine.resolve_override).
    """
    rule = get_rule(CLUSTER_OFFLINE_RULE)
    effective = resolve_override(
        cluster.owner_id, rule.id, cluster, overrides=list(overrides), scope="cluster"
    )
    if not effective.enabled:
        return None
    if effective.threshold_overridden:
        return effective.threshold
    return max(rule.default_threshold, silent_after_seconds)


def cluster_silence_reason(
    cluster: Cluster, now: datetime, silent_after_seconds: int, overrides: Iterable[Any]
) -> str | None:
    """Why this cluster counts as offline right now, or None. Never reported = waiting."""
    if cluster.last_seen_at is None or cluster.latest_snapshot is None:
        return None
    threshold = cluster_offline_threshold(cluster, silent_after_seconds, overrides)
    if threshold is None:
        return None
    silent = (now - cluster.last_seen_at).total_seconds()
    if silent > threshold:
        return f"No cluster data for {duration(silent)}"
    return None


def effective_cluster_status(
    cluster: Cluster, now: datetime, silent_after_seconds: int, overrides: Iterable[Any]
) -> str:
    """The status to show right now, even when the scheduler isn't running."""
    if cluster.latest_snapshot is None:
        return WAITING
    if cluster_silence_reason(cluster, now, silent_after_seconds, overrides):
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
    from rules.models import RuleOverride

    if now is None:
        now = timezone.now()
    default_silent = Prefs._meta.get_field("silent_after_seconds").default
    silent_after = dict(Prefs.objects.values_list("owner_id", "silent_after_seconds"))
    # One query for every owner's overrides of this rule, not one per cluster.
    overrides = defaultdict(list)
    for override in RuleOverride.objects.filter(rule_id=CLUSTER_OFFLINE_RULE):
        overrides[override.owner_id].append(override)

    candidates = Cluster.objects.exclude(status__in=[Host.Status.WAITING, Host.Status.OFFLINE])
    marked = []
    for cluster_id, owner_id in candidates.values_list("pk", "owner_id"):
        threshold = silent_after.get(owner_id, default_silent)
        with transaction.atomic():
            # Re-read under a lock: a snapshot may have arrived since the query above.
            cluster = Cluster.objects.select_for_update().filter(pk=cluster_id).first()
            if cluster is None or cluster.status in (Host.Status.OFFLINE, Host.Status.WAITING):
                continue
            reason = cluster_silence_reason(cluster, now, threshold, overrides[owner_id])
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
