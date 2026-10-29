"""Kubernetes rules: each one has a positive and a negative case, a plain why and a safe fix."""

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from rules.base import fingerprint
from rules.engine import (
    apply_suppressions,
    compute_status,
    evaluate,
    evaluate_cluster,
    resolve_override,
)
from rules.k8s import CLUSTER_RULES
from rules.registry import RULES, get_rule

from .sample import make_cluster, make_report

NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)
OWNER = 10
CLUSTER_ID = 7

K3S_LATEST = {
    "k3s": {"stable": "v1.33.1+k3s1", "v1.31": "v1.31.9+k3s1", "v1.33": "v1.33.1+k3s1"},
    "argocd": {"latest": "v3.6.0"},
    "helm": {"latest": "v3.19.0"},
}


def fake_cluster(snapshot=None, **changes):
    values = {
        "pk": CLUSTER_ID,
        "owner_id": OWNER,
        "name": "lab",
        "status": "green",
        "latest_snapshot": make_cluster() if snapshot is None else snapshot,
        "last_seen_at": NOW,
    }
    values["latest_report"] = values["latest_snapshot"]
    values.update(changes)
    return SimpleNamespace(**values)


def run(snapshot=None, overrides=(), latest=None, cluster=None, now=NOW):
    cluster = cluster or fake_cluster(snapshot)
    return evaluate_cluster(
        cluster, cluster.latest_snapshot, now, overrides=overrides, latest=latest or {}
    )


def only(findings, rule_id):
    return [f for f in findings if f.rule_id == rule_id]


def pod(**changes):
    values = {
        "ns": "apps",
        "name": "web-5d9f7c8b6-x2k4q",
        "node": "worker-1",
        "phase": "Running",
        "ready": [0, 1],
        "restarts": 0,
        "reason": None,
        "last": None,
        "last_age_s": None,
        "age_s": 86400,
        "owner": "Deployment/web",
    }
    values.update(changes)
    return values


def workload(**changes):
    values = {
        "kind": "Deployment",
        "ns": "apps",
        "name": "web",
        "desired": 2,
        "ready": 2,
        "available": 2,
        "updated": 2,
        "misscheduled": 0,
        "images": ["ghcr.io/example/web:1.4.2"],
        "helm": None,
    }
    values.update(changes)
    return values


def node(**changes):
    values = dict(make_cluster()["nodes"][0])
    values.update(changes)
    return values


def override(rule_id, cluster_id=CLUSTER_ID, subject="", **fields):
    values = {"enabled": None, "severity": "", "threshold": None, "notify": ""}
    values.update(fields)
    return SimpleNamespace(
        owner_id=OWNER,
        rule_id=rule_id,
        host_id=None,
        cluster_id=cluster_id,
        subject=subject,
        **values,
    )


# --- the registry -------------------------------------------------------------------------


def test_a_healthy_cluster_has_no_findings():
    assert run() == []


def test_every_cluster_rule_is_kubernetes_with_why_and_a_read_only_fix():
    assert len(CLUSTER_RULES) >= 22
    for rule in CLUSTER_RULES.values():
        assert rule.id.startswith("k8s.")
        assert rule.category == "kubernetes"
        assert len(rule.why) > 30
        assert get_rule(rule.id) is rule
        if rule.id != "k8s.snapshot_truncated":
            assert rule.fix_template, rule.id
        # read-only and copy-only: nothing that changes the cluster
        for word in ("delete", "apply", "patch", "edit", "scale", "rollout", "rm ", "exec"):
            assert word not in rule.fix_template, rule.id


def test_the_helm_cli_rule_is_a_host_rule():
    rule = RULES["k8s.helm_cli_outdated"]
    assert rule.default_severity == "info"
    assert rule.category == "kubernetes"
    assert "k8s.helm_cli_outdated" not in CLUSTER_RULES


# --- cluster-level rules ----------------------------------------------------------------------


def test_cluster_offline():
    stale = fake_cluster(last_seen_at=NOW - timedelta(seconds=181))
    [finding] = only(run(cluster=stale), "k8s.cluster_offline")
    assert finding.severity == "red"
    assert "3 min" in finding.text
    assert compute_status([finding], stale, NOW) == "offline"

    fresh = fake_cluster(last_seen_at=NOW - timedelta(seconds=180))
    assert only(run(cluster=fresh), "k8s.cluster_offline") == []
    never = fake_cluster(last_seen_at=None)
    assert only(run(cluster=never), "k8s.cluster_offline") == []


@pytest.mark.parametrize("error", ["refused", "timeout", "tls", "bad_response"])
def test_api_unreachable(error):
    findings = run(make_cluster(api={"error": error, "cert_days": None}))
    [finding] = only(findings, "k8s.api_unreachable")
    assert finding.severity == "red"
    assert "systemctl status k3s" in finding.fix


def test_api_unreachable_is_quiet_for_unauthorized_and_ok():
    for error in (None, "unauthorized"):
        findings = run(make_cluster(api={"error": error, "cert_days": 200}))
        assert only(findings, "k8s.api_unreachable") == []


def test_token_rejected_uses_the_service_account_in_the_fix():
    findings = run(make_cluster(api={"error": "unauthorized", "cert_days": None}))
    [finding] = only(findings, "k8s.token_rejected")
    assert finding.severity == "red"
    assert finding.fix == "kubectl -n monitoring get serviceaccount cronrack-agent"


def test_token_rejected_without_a_known_service_account_has_a_safe_fix():
    findings = run(make_cluster(api={"error": "unauthorized", "cert_days": None}, sa=None))
    [finding] = only(findings, "k8s.token_rejected")
    assert finding.fix == "kubectl get serviceaccounts -A"


def test_hostile_service_account_never_reaches_the_fix():
    sa = "system:serviceaccount:x;rm:-rf"
    findings = run(make_cluster(api={"error": "unauthorized", "cert_days": None}, sa=sa))
    [finding] = only(findings, "k8s.token_rejected")
    assert finding.fix == "kubectl get serviceaccounts -A"


@pytest.mark.parametrize(("days", "fires"), [(13, True), (14, False), (0, True), (-1, True)])
def test_token_expiring(days, fires):
    findings = only(run(make_cluster(token_days=days)), "k8s.token_expiring")
    assert bool(findings) is fires
    if fires:
        assert findings[0].severity == "yellow"


def test_token_expiring_is_quiet_without_expiry():
    assert only(run(make_cluster(token_days=None)), "k8s.token_expiring") == []


def test_token_expiring_threshold_can_be_overridden():
    rule = "k8s.token_expiring"
    findings = run(make_cluster(token_days=20), overrides=[override(rule, threshold=30)])
    assert len(only(findings, rule)) == 1


def test_permission_denied_one_finding_per_entry():
    denied = [
        {"g": "", "r": "pods", "v": "list"},
        {"g": "argoproj.io", "r": "applications", "v": "list"},
    ]
    findings = only(run(make_cluster(denied=denied)), "k8s.permission_denied")
    assert [f.subject for f in findings] == [
        "rbac:/pods/list",
        "rbac:argoproj.io/applications/list",
    ]
    assert all(f.severity == "yellow" for f in findings)
    assert "applications.argoproj.io" in findings[1].fix
    assert "pods" in findings[0].fix


def test_node_not_ready_and_pressure():
    nodes = [
        node(),
        node(name="worker-1", ready=False),
        node(name="worker-2", pressure=["DiskPressure"]),
    ]
    findings = run(make_cluster(nodes=nodes))
    [down] = only(findings, "k8s.node_not_ready")
    assert (down.severity, down.subject) == ("red", "node:worker-1")
    assert down.fix == "kubectl describe node worker-1"
    [pressure] = only(findings, "k8s.node_pressure")
    assert (pressure.severity, pressure.subject) == ("yellow", "node:worker-2")
    assert "DiskPressure" in pressure.text


# --- pods -----------------------------------------------------------------------------------------


def pods_run(*pods, **cluster):
    return run(make_cluster(problem_pods=list(pods), **cluster))


def test_pod_crashloop():
    [finding] = only(pods_run(pod(reason="CrashLoopBackOff")), "k8s.pod_crashloop")
    assert finding.severity == "red"
    assert finding.subject == "pod:apps/Deployment/web"
    assert "web-5d9f7c8b6-x2k4q" in finding.text
    assert finding.fix == (
        "kubectl -n apps logs web-5d9f7c8b6-x2k4q --previous --tail=50 --all-containers"
    )
    assert only(pods_run(pod(reason="ImagePullBackOff")), "k8s.pod_crashloop") == []


def test_pod_fingerprint_survives_a_rollout():
    first = only(pods_run(pod(name="web-aaa-1", reason="CrashLoopBackOff")), "k8s.pod_crashloop")
    second = only(pods_run(pod(name="web-bbb-2", reason="CrashLoopBackOff")), "k8s.pod_crashloop")
    assert first[0].fingerprint == second[0].fingerprint
    assert first[0].text != second[0].text


def test_several_pods_of_one_workload_make_one_finding():
    findings = pods_run(
        pod(name="web-a", reason="CrashLoopBackOff"),
        pod(name="web-b", reason="CrashLoopBackOff"),
        pod(name="db-0", owner="StatefulSet/db", reason="CrashLoopBackOff"),
    )
    found = only(findings, "k8s.pod_crashloop")
    assert [f.subject for f in found] == ["pod:apps/Deployment/web", "pod:apps/StatefulSet/db"]
    assert "1 more" in found[0].text


def test_long_pod_subjects_fall_back_to_a_hash():
    long_owner = "Deployment/" + "w" * 190
    [finding] = only(
        pods_run(pod(owner=long_owner, reason="CrashLoopBackOff")), "k8s.pod_crashloop"
    )
    digest = hashlib.sha256(f"pod:apps/{long_owner}".encode()).hexdigest()[:16]
    assert finding.subject == f"pod:#{digest}"
    assert len(finding.subject) <= 200


@pytest.mark.parametrize(
    ("last", "age", "fires"),
    [
        ("OOMKilled", 120, True),
        ("OOMKilled", 86_399, True),
        ("OOMKilled", 86_400, False),
        ("OOMKilled", None, False),
        ("Error", 120, False),
        (None, None, False),
    ],
)
def test_pod_oomkilled(last, age, fires):
    findings = only(pods_run(pod(last=last, last_age_s=age)), "k8s.pod_oomkilled")
    assert bool(findings) is fires
    if fires:
        assert findings[0].severity == "red"
        assert findings[0].fix == "kubectl -n apps describe pod web-5d9f7c8b6-x2k4q"


def test_pod_config_error_is_red():
    [finding] = only(pods_run(pod(reason="CreateContainerConfigError")), "k8s.pod_config_error")
    assert finding.severity == "red"


@pytest.mark.parametrize("reason", ["ImagePullBackOff", "ErrImagePull", "InvalidImageName"])
def test_pod_image_pull(reason):
    [finding] = only(pods_run(pod(phase="Pending", reason=reason)), "k8s.pod_image_pull")
    assert finding.severity == "yellow"


@pytest.mark.parametrize(
    ("phase", "reason", "age", "fires"),
    [
        ("Pending", None, 301, True),
        ("Pending", None, 300, False),
        ("Pending", None, None, False),
        ("Pending", "Unschedulable", 900, True),
        ("Pending", "ImagePullBackOff", 900, False),
        ("Pending", "CreateContainerConfigError", 900, False),
        ("Running", None, 900, False),
    ],
)
def test_pod_pending(phase, reason, age, fires):
    findings = only(pods_run(pod(phase=phase, reason=reason, age_s=age)), "k8s.pod_pending")
    assert bool(findings) is fires


@pytest.mark.parametrize(
    ("restarts", "age", "fires"),
    [(6, 600, True), (5, 600, False), (50, 86_400, False), (50, None, False), (50, 86_399, True)],
)
def test_pod_restarts(restarts, age, fires):
    findings = only(pods_run(pod(restarts=restarts, last_age_s=age)), "k8s.pod_restarts")
    assert bool(findings) is fires
    if fires:
        assert findings[0].severity == "yellow"


def test_pod_restart_threshold_override():
    rule = "k8s.pod_restarts"
    findings = run(
        make_cluster(problem_pods=[pod(restarts=6, last_age_s=60)]),
        overrides=[override(rule, threshold=10)],
    )
    assert only(findings, rule) == []


# --- workloads ------------------------------------------------------------------------------------


def workloads_run(*items):
    return run(make_cluster(workloads=list(items)))


@pytest.mark.parametrize("kind", ["Deployment", "StatefulSet"])
def test_workload_unavailable(kind):
    findings = workloads_run(workload(kind=kind, desired=2, available=0, ready=0))
    [finding] = only(findings, "k8s.workload_unavailable")
    assert finding.severity == "red"
    assert finding.subject == f"workload:apps/{kind}/web"
    assert only(findings, "k8s.workload_degraded") == []


def test_workload_scaled_to_zero_is_fine():
    assert workloads_run(workload(desired=0, available=0, ready=0)) == []


def test_workload_degraded():
    findings = workloads_run(workload(desired=3, available=1))
    [finding] = only(findings, "k8s.workload_degraded")
    assert finding.severity == "yellow"
    assert "1/3" in finding.text
    assert only(findings, "k8s.workload_unavailable") == []


def test_daemonset_incomplete():
    ds = workload(kind="DaemonSet", name="agent", desired=3, available=2)
    [finding] = only(workloads_run(ds), "k8s.daemonset_incomplete")
    assert finding.severity == "yellow"
    assert finding.fix == "kubectl -n apps describe daemonset agent"
    # daemonsets do not use the deployment rules
    assert only(workloads_run(ds), "k8s.workload_degraded") == []


def test_daemonset_misscheduled_counts():
    ds = workload(kind="DaemonSet", name="agent", desired=3, available=3, misscheduled=1)
    assert len(only(workloads_run(ds), "k8s.daemonset_incomplete")) == 1
    ok = workload(kind="DaemonSet", name="agent", desired=3, available=3)
    assert workloads_run(ok) == []


@pytest.mark.parametrize(
    ("images", "fires"),
    [
        (["nginx:latest"], True),
        (["nginx"], True),
        (["registry.example:5000/nginx"], True),
        (["nginx:1.27"], False),
        (["nginx:latest@sha256:" + "a" * 64], False),
        (["registry.example:5000/nginx:1.27"], False),
        ([], False),
    ],
)
def test_image_unpinned(images, fires):
    findings = only(workloads_run(workload(images=images)), "k8s.image_unpinned")
    assert bool(findings) is fires
    if fires:
        assert findings[0].severity == "yellow"
        assert findings[0].fix == (
            "kubectl -n apps get Deployment web -o jsonpath='{.spec.template.spec.containers[*].image}'"
        )


def test_hostile_workload_names_are_quoted_in_fixes():
    findings = workloads_run(workload(name="x; rm -rf ~", desired=2, available=0))
    [finding] = only(findings, "k8s.workload_unavailable")
    assert "'x; rm -rf ~'" in finding.fix


# --- storage, Argo CD ------------------------------------------------------------------------------


def pvc(**changes):
    values = {
        "ns": "apps",
        "name": "data-web-0",
        "phase": "Pending",
        "class": "local-path",
        "size": "1Gi",
        "age_s": 601,
    }
    values.update(changes)
    return values


@pytest.mark.parametrize(
    ("phase", "age", "fires"),
    [
        ("Pending", 601, True),
        ("Pending", 600, False),
        ("Pending", None, False),
        ("Bound", 9999, False),
    ],
)
def test_pvc_pending(phase, age, fires):
    findings = only(run(make_cluster(pvcs=[pvc(phase=phase, age_s=age)])), "k8s.pvc_pending")
    assert bool(findings) is fires
    if fires:
        assert findings[0].subject == "pvc:apps/data-web-0"
        assert findings[0].fix == "kubectl -n apps describe pvc data-web-0"


def argo(**changes):
    values = {
        "name": "web",
        "ns": "argocd",
        "project": "default",
        "sync": "Synced",
        "health": "Healthy",
        "rev": "a1b2c3d",
        "op": "Succeeded",
        "synced_at": None,
    }
    values.update(changes)
    return values


def test_argo_out_of_sync():
    findings = run(make_cluster(argo=[argo(sync="OutOfSync")]))
    [finding] = only(findings, "k8s.argo_out_of_sync")
    assert (finding.severity, finding.subject) == ("yellow", "argo:argocd/web")
    assert finding.fix == "kubectl -n argocd describe application web"
    assert only(run(make_cluster(argo=[argo()])), "k8s.argo_out_of_sync") == []
    assert only(run(make_cluster(argo=[argo(sync="Unknown")])), "k8s.argo_out_of_sync") == []


@pytest.mark.parametrize(
    ("health", "fires"),
    [
        ("Degraded", True),
        ("Missing", True),
        ("Progressing", False),
        ("Healthy", False),
        ("Suspended", False),
        ("Unknown", False),
    ],
)
def test_argo_unhealthy(health, fires):
    findings = only(run(make_cluster(argo=[argo(health=health)])), "k8s.argo_unhealthy")
    assert bool(findings) is fires
    if fires:
        assert findings[0].severity == "red"


# --- certificate, truncation ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "severity"),
    [(31, None), (30, None), (29, "yellow"), (7, "yellow"), (6, "red"), (0, "red"), (None, None)],
)
def test_api_cert_expiring(days, severity):
    findings = only(
        run(make_cluster(api={"error": None, "cert_days": days})), "k8s.api_cert_expiring"
    )
    assert [f.severity for f in findings] == ([severity] if severity else [])


def test_snapshot_truncated_is_info_and_does_not_change_the_colour():
    cluster = fake_cluster(make_cluster(truncated=True, cut=["ok_pods", "events"]))
    findings = run(cluster=cluster)
    [finding] = only(findings, "k8s.snapshot_truncated")
    assert finding.severity == "info"
    assert "ok_pods" in finding.text
    assert compute_status(findings, cluster, NOW) == "green"
    assert only(run(make_cluster(truncated=False)), "k8s.snapshot_truncated") == []


# --- versions (only with "latest" data, i.e. when online checks are on) ---------------------------------


def k3s_nodes(version):
    return [node(kubelet=version), node(name="worker-1", kubelet=version)]


def test_version_rules_are_quiet_without_latest_data():
    snapshot = make_cluster(nodes=k3s_nodes("v1.20.0+k3s1"), argocd_version="v1.0.0")
    findings = run(snapshot)
    for rule_id in ("k8s.k3s_patch_behind", "k8s.k3s_minor_behind", "k8s.argocd_outdated"):
        assert only(findings, rule_id) == []


def test_k3s_patch_behind_is_yellow():
    snapshot = make_cluster(version="v1.31.2+k3s1", nodes=k3s_nodes("v1.31.2+k3s1"))
    findings = run(snapshot, latest=K3S_LATEST)
    [finding] = only(findings, "k8s.k3s_patch_behind")
    assert finding.severity == "yellow"
    assert "v1.31.9+k3s1" in finding.text
    assert finding.fix == "kubectl get nodes -o wide"
    # and the older minor is also behind stable, but that one is only information
    [minor] = only(findings, "k8s.k3s_minor_behind")
    assert minor.severity == "info"
    assert "v1.33.1+k3s1" in minor.text


def test_k3s_up_to_date_is_quiet():
    snapshot = make_cluster(version="v1.33.1+k3s1", nodes=k3s_nodes("v1.33.1+k3s1"))
    findings = run(snapshot, latest=K3S_LATEST)
    assert only(findings, "k8s.k3s_patch_behind") == []
    assert only(findings, "k8s.k3s_minor_behind") == []


def test_k3s_newer_than_the_channel_is_quiet():
    snapshot = make_cluster(version="v1.31.12+k3s1", nodes=k3s_nodes("v1.31.12+k3s1"))
    assert only(run(snapshot, latest=K3S_LATEST), "k8s.k3s_patch_behind") == []


def test_k3s_revision_counts_as_behind():
    snapshot = make_cluster(version="v1.33.1+k3s0", nodes=k3s_nodes("v1.33.1+k3s0"))
    assert len(only(run(snapshot, latest=K3S_LATEST), "k8s.k3s_patch_behind")) == 1


def test_k3s_with_no_channel_for_its_minor_is_quiet():
    snapshot = make_cluster(version="v1.29.1+k3s1", nodes=k3s_nodes("v1.29.1+k3s1"))
    assert only(run(snapshot, latest=K3S_LATEST), "k8s.k3s_patch_behind") == []


def test_no_version_check_for_other_distributions():
    snapshot = make_cluster(version="v1.20.0", nodes=k3s_nodes("v1.20.0"))
    findings = run(snapshot, latest=K3S_LATEST)
    assert only(findings, "k8s.k3s_patch_behind") == []
    assert only(findings, "k8s.k3s_minor_behind") == []


def test_garbage_latest_data_is_ignored():
    snapshot = make_cluster(version="v1.31.2+k3s1", nodes=k3s_nodes("v1.31.2+k3s1"))
    findings = run(snapshot, latest={"k3s": {"v1.31": "nonsense", "stable": 5}, "argocd": "x"})
    assert findings == []


def test_argocd_outdated():
    snapshot = make_cluster(argocd_version="v3.5.6")
    [finding] = only(run(snapshot, latest=K3S_LATEST), "k8s.argocd_outdated")
    assert finding.severity == "yellow"
    assert "v3.6.0" in finding.text
    assert (
        only(run(make_cluster(argocd_version="v3.6.0"), latest=K3S_LATEST), "k8s.argocd_outdated")
        == []
    )
    assert (
        only(run(make_cluster(argocd_version=None), latest=K3S_LATEST), "k8s.argocd_outdated") == []
    )
    assert only(run(make_cluster(argocd_version="3.5.6"), latest=K3S_LATEST), "k8s.argocd_outdated")


def host_with(helm):
    report = make_report(
        kubernetes={
            "node": {
                "role": "none",
                "dist": None,
                "name": "nas",
                "k3s": None,
                "kubelet": None,
                "helm": helm,
            },
            "cluster": None,
        }
    )
    return SimpleNamespace(
        pk=1,
        owner_id=OWNER,
        connected=True,
        last_seen_at=NOW,
        learning_until=None,
        maintenance_until=None,
        latest_report=report,
    ), report


def test_helm_cli_outdated():
    host, report = host_with("v3.16.2")
    findings = evaluate(host, report, NOW, overrides=[], latest=K3S_LATEST)
    [finding] = only(findings, "k8s.helm_cli_outdated")
    assert finding.severity == "info"
    assert "v3.19.0" in finding.text
    assert finding.fix == "helm version --short"
    assert compute_status(findings, host, NOW) == "green"

    assert only(evaluate(host, report, NOW, overrides=[]), "k8s.helm_cli_outdated") == []
    host, report = host_with("v3.19.0")
    assert (
        only(evaluate(host, report, NOW, overrides=[], latest=K3S_LATEST), "k8s.helm_cli_outdated")
        == []
    )
    host, report = host_with(None)
    assert (
        only(evaluate(host, report, NOW, overrides=[], latest=K3S_LATEST), "k8s.helm_cli_outdated")
        == []
    )


def test_hosts_without_the_kubernetes_key_are_unaffected():
    host, _ = host_with(None)
    assert (
        only(
            evaluate(host, make_report(), NOW, overrides=[], latest=K3S_LATEST),
            "k8s.helm_cli_outdated",
        )
        == []
    )


# --- overrides, suppressions and fingerprints -------------------------------------------------------


def test_cluster_fingerprints_are_scoped_to_the_cluster():
    [finding] = only(pods_run(pod(reason="CrashLoopBackOff")), "k8s.pod_crashloop")
    assert finding.fingerprint == fingerprint(
        "k8s.pod_crashloop", f"c{CLUSTER_ID}", "pod:apps/Deployment/web"
    )
    # a host with the same number never shares it
    assert finding.fingerprint != fingerprint(
        "k8s.pod_crashloop", CLUSTER_ID, "pod:apps/Deployment/web"
    )


def test_rule_can_be_disabled_or_reclassified_per_cluster():
    snapshot = make_cluster(problem_pods=[pod(reason="CrashLoopBackOff")])
    off = run(snapshot, overrides=[override("k8s.pod_crashloop", enabled=False)])
    assert only(off, "k8s.pod_crashloop") == []
    info = run(snapshot, overrides=[override("k8s.pod_crashloop", severity="info")])
    assert only(info, "k8s.pod_crashloop")[0].severity == "info"


def test_subject_override_beats_the_whole_rule():
    snapshot = make_cluster(
        problem_pods=[
            pod(reason="CrashLoopBackOff"),
            pod(ns="x", name="db", owner="StatefulSet/db", reason="CrashLoopBackOff"),
        ]
    )
    findings = run(
        snapshot,
        overrides=[override("k8s.pod_crashloop", subject="pod:x/StatefulSet/db", enabled=False)],
    )
    assert [f.subject for f in only(findings, "k8s.pod_crashloop")] == ["pod:apps/Deployment/web"]


def test_overrides_of_another_cluster_or_a_host_do_not_apply():
    snapshot = make_cluster(problem_pods=[pod(reason="CrashLoopBackOff")])
    other = override("k8s.pod_crashloop", cluster_id=CLUSTER_ID + 1, enabled=False)
    host_level = SimpleNamespace(
        owner_id=OWNER,
        rule_id="k8s.pod_crashloop",
        host_id=CLUSTER_ID,
        cluster_id=None,
        subject="",
        enabled=False,
        severity="",
        threshold=None,
        notify="",
    )
    assert len(only(run(snapshot, overrides=[other, host_level]), "k8s.pod_crashloop")) == 1


def test_resolve_override_cluster_scope_ignores_other_owners():
    foreign = override("k8s.token_expiring", threshold=99)
    foreign.owner_id = OWNER + 1
    effective = resolve_override(
        OWNER, "k8s.token_expiring", CLUSTER_ID, overrides=[foreign], scope="cluster"
    )
    assert effective.threshold == 14


def test_per_cluster_suppression_hides_the_finding():
    snapshot = make_cluster(problem_pods=[pod(reason="CrashLoopBackOff")])
    cluster = fake_cluster(snapshot)
    findings = run(cluster=cluster)
    [crash] = only(findings, "k8s.pod_crashloop")
    mine = SimpleNamespace(
        fingerprint=crash.fingerprint,
        kind="expected",
        expires_at=None,
        host_id=None,
        cluster_id=CLUSTER_ID,
        owner_id=OWNER,
    )
    other_cluster = SimpleNamespace(**{**vars(mine), "cluster_id": CLUSTER_ID + 1})
    other_owner = SimpleNamespace(**{**vars(mine), "owner_id": OWNER + 1})
    for useless in (other_cluster, other_owner):
        active, _ = apply_suppressions(
            findings, cluster, NOW, suppressions=[useless], scope="cluster"
        )
        assert crash in active
    active, suppressed = apply_suppressions(
        findings, cluster, NOW, suppressions=[mine], scope="cluster"
    )
    assert crash in suppressed
    assert compute_status(active, cluster, NOW) == "green"


def test_host_fingerprints_do_not_change():
    """The cluster work must not change what a host's fingerprint is (suppressions point at it)."""
    host = SimpleNamespace(
        pk=3,
        owner_id=OWNER,
        connected=True,
        last_seen_at=NOW,
        learning_until=None,
        maintenance_until=None,
        latest_report=None,
    )
    report = make_report(updates={"total": 5, "security": 2}, disks=[{"mount": "/", "percent": 99}])
    findings = {f.rule_id: f for f in evaluate(host, report, NOW, overrides=[])}
    expected = hashlib.sha256(b"updates.security_pending|3|").hexdigest()
    assert findings["updates.security_pending"].fingerprint == expected
    expected = hashlib.sha256(b"disk.usage|3|mount:/").hexdigest()
    assert findings["disk.usage"].fingerprint == expected
