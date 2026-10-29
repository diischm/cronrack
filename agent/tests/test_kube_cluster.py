"""Turning Kubernetes API JSON into the compact cluster view the agent reports.

The JSON in fixtures/k8s/ is a sanitized capture from a small k3d lab (see the README
there). Secret-looking "SENTINEL-SECRET-n" strings were planted in the places a token
can read but the agent must never copy: env values, Helm values, Argo sources, annotations.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent import kube_api, kube_cluster
from agent.kube_api import (
    ApiError,
    Denied,
    NotInstalled,
    Unauthorized,
)

FIXTURES = Path(__file__).parent / "fixtures" / "k8s"
NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)
# Right after the lab capture was taken (2026-10-28 23:24 UTC).
LAB_NOW = datetime(2026, 10, 29, 0, 0, 0, tzinfo=UTC)
KUBE_SYSTEM_UID = "08528ee9-428d-5167-8a8e-df19e635932f"  # fake, from the sanitizer


def load(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def by_name(items: list[dict], name: str) -> dict:
    [item] = [i for i in items if i["name"] == name]
    return item


# --- small parsers ----------------------------------------------------------------------


def test_namespace_uid_is_a_uuid_or_none():
    uid = kube_cluster.parse_namespace_uid(load("namespace_kube_system"))
    assert uid is not None
    assert len(uid) == 36
    assert kube_cluster.parse_namespace_uid({"metadata": {"uid": "not-a-uuid"}}) is None


def test_version():
    assert kube_cluster.parse_version(load("version")) == "v1.35.5+k3s1"
    assert kube_cluster.parse_version({"gitVersion": "garbage; rm -rf /"}) is None


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("16", 16.0),
        ("250m", 0.25),
        ("64587472n", 0.064587472),
        ("1500u", 0.0015),
        ("1Ki", 1024.0),
        ("470300Ki", 470300 * 1024.0),
        ("1Gi", 2.0**30),
        ("1.5Gi", 1.5 * 2**30),
        ("2k", 2000.0),
        ("5M", 5e6),
        ("3G", 3e9),
    ],
)
def test_parse_quantity(text, value):
    assert kube_cluster.parse_quantity(text) == pytest.approx(value)


@pytest.mark.parametrize(
    "text", ["", "abc", "1Zi", "1e400", "-5", "nan", "inf", "1 Gi", None, 5, []]
)
def test_parse_quantity_rejects_garbage(text):
    assert kube_cluster.parse_quantity(text) is None


# --- nodes ----------------------------------------------------------------------------------


def test_parse_nodes_fixture():
    nodes = kube_cluster.parse_nodes(load("nodes"))

    assert [n["name"] for n in nodes] == ["server-1", "worker-1", "worker-2"]
    server = by_name(nodes, "server-1")
    assert set(server) == {
        "name",
        "roles",
        "ready",
        "pressure",
        "kubelet",
        "os",
        "arch",
        "cpu_m",
        "mem_mi",
        "cpu_used_m",
        "mem_used_mi",
    }
    assert server["roles"] == ["control-plane"]
    assert by_name(nodes, "worker-1")["roles"] == []
    assert server["ready"] is True
    assert server["pressure"] == []
    assert server["kubelet"] == "v1.35.5+k3s1"
    assert (server["os"], server["arch"]) == ("linux", "amd64")
    assert server["cpu_m"] == 16000
    assert server["mem_mi"] == round(13985260 / 1024)
    assert server["cpu_used_m"] is None  # no metrics given


def test_parse_nodes_with_metrics():
    metrics = kube_cluster.parse_node_metrics(load("node_metrics"))
    nodes = kube_cluster.parse_nodes(load("nodes"), metrics)

    worker = by_name(nodes, "worker-1")
    assert worker["cpu_used_m"] == 65  # 64587472n
    assert worker["mem_used_mi"] == round(470300 / 1024)


def test_parse_node_metrics_fixture():
    metrics = kube_cluster.parse_node_metrics(load("node_metrics"))
    assert set(metrics) == {"server-1", "worker-1", "worker-2"}
    assert set(metrics["server-1"]) == {"cpu_used_m", "mem_used_mi"}


def test_node_not_ready_and_pressure_sort_first():
    data = {
        "items": [
            {
                "metadata": {"name": "a-ok"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            },
            {
                "metadata": {"name": "b-bad", "labels": {"node-role.kubernetes.io/worker": ""}},
                "status": {
                    "conditions": [
                        {"type": "Ready", "status": "False"},
                        {"type": "MemoryPressure", "status": "True"},
                        {"type": "DiskPressure", "status": "False"},
                        {"type": "PIDPressure", "status": "True"},
                    ]
                },
            },
        ]
    }

    nodes = kube_cluster.parse_nodes(data)

    assert [n["name"] for n in nodes] == ["b-bad", "a-ok"]
    assert nodes[0]["ready"] is False
    assert nodes[0]["pressure"] == ["MemoryPressure", "PIDPressure"]
    assert nodes[0]["roles"] == ["worker"]


def test_node_without_ready_condition_is_not_ready():
    [node] = kube_cluster.parse_nodes({"items": [{"metadata": {"name": "n1"}}]})
    assert node["ready"] is False


# --- pods ---------------------------------------------------------------------------------


def test_parse_pods_broken_fixture():
    pods = kube_cluster.parse_pods(load("pods_broken"), NOW)
    by = {p["name"]: p for p in pods}

    crash = by["web-5d9f7c8b6-crash1"]
    assert set(crash) == {
        "ns",
        "name",
        "node",
        "phase",
        "ready",
        "restarts",
        "reason",
        "last",
        "last_age_s",
        "age_s",
        "owner",
    }
    assert crash["ns"] == "apps"
    assert crash["node"] == "worker-1"
    assert crash["phase"] == "Running"
    assert crash["ready"] == [0, 1]
    assert crash["restarts"] == 14
    assert crash["reason"] == "CrashLoopBackOff"
    assert crash["last"] == "Error"
    assert crash["last_age_s"] == 120
    assert crash["age_s"] == 86400
    assert crash["owner"] == "Deployment/web"

    assert by["web-5d9f7c8b6-pull1"]["reason"] == "ImagePullBackOff"
    assert by["api-7c6b9d5f4-pull2"]["reason"] == "ErrImagePull"
    assert by["api-7c6b9d5f4-pull2"]["owner"] == "Deployment/api"
    assert by["worker-6b7d8c9f5-cfg1"]["reason"] == "CreateContainerConfigError"

    oom = by["db-0"]
    assert oom["reason"] is None
    assert oom["last"] == "OOMKilled"
    assert oom["last_age_s"] == 3660
    assert oom["ready"] == [1, 1]
    assert oom["owner"] == "StatefulSet/db"

    pending = by["batch-1-pending"]
    assert pending["phase"] == "Pending"
    assert pending["node"] is None
    assert pending["reason"] == "Unschedulable"
    assert pending["age_s"] == 2 * 86400
    assert pending["owner"] == "Job/batch-1"
    assert pending["ready"] == [0, 1]

    evicted = by["old-6f8d7b5c4-evicted"]
    assert (evicted["phase"], evicted["reason"]) == ("Failed", "Evicted")

    fine = by["web-5d9f7c8b6-fine1"]
    assert (fine["reason"], fine["last"], fine["restarts"]) == (None, None, 0)


def test_parse_pods_lab_fixture():
    pods = kube_cluster.parse_pods(load("pods"), LAB_NOW)

    assert len(pods) == 17
    by = {p["name"]: p for p in pods}
    redis = by["argocd-redis-5f4645998-f67bc"]
    assert redis["owner"] == "Deployment/argocd-redis"
    assert redis["ns"] == "argocd"
    assert (
        by["argocd-application-controller-0"]["owner"]
        == "StatefulSet/argocd-application-controller"
    )
    assert by["helm-install-traefik-rhdhp"]["phase"] == "Succeeded"
    assert by["svclb-traefik-36fe05f0-5m2fb"]["owner"] == "DaemonSet/svclb-traefik-36fe05f0"
    assert by["svclb-traefik-36fe05f0-5m2fb"]["ready"] == [2, 2]


def test_owner_of():
    def pod(owner, labels=None):
        return {
            "metadata": {
                "name": "p-1",
                "labels": labels or {},
                "ownerReferences": [owner] if owner else [],
            }
        }

    rs = {"kind": "ReplicaSet", "name": "web-5d9f7c8b6"}
    assert kube_cluster.owner_of(pod(rs, {"pod-template-hash": "5d9f7c8b6"})) == "Deployment/web"
    # Without the hash label we can't know the ReplicaSet belongs to a Deployment.
    assert kube_cluster.owner_of(pod(rs)) == "ReplicaSet/web-5d9f7c8b6"
    # A hash that isn't the name's suffix is not trusted either.
    assert (
        kube_cluster.owner_of(pod(rs, {"pod-template-hash": "zzz"})) == "ReplicaSet/web-5d9f7c8b6"
    )
    assert kube_cluster.owner_of(pod({"kind": "StatefulSet", "name": "db"})) == "StatefulSet/db"
    assert kube_cluster.owner_of(pod({"kind": "Job", "name": "j"})) == "Job/j"
    assert kube_cluster.owner_of(pod(None)) == "Pod/p-1"
    assert kube_cluster.owner_of({}) == "Pod/unknown"
    assert kube_cluster.owner_of({"metadata": {"ownerReferences": "x"}}) == "Pod/unknown"


def test_pod_counts_and_problem_split():
    pods = kube_cluster.parse_pods(load("pods_broken"), NOW)

    problems, ok, counts = kube_cluster.split_pods(pods)

    assert counts == {
        "total": 8,
        "running": 3,
        "pending": 4,
        "succeeded": 0,
        "failed": 1,
        "problem": 7,
    }
    assert {p["name"] for p in problems} == {p["name"] for p in pods} - {"web-5d9f7c8b6-fine1"}
    assert ok == {"apps": [["web-5d9f7c8b6-fine1", "worker-1", 0, 86400]]}


def test_problem_pods_are_ordered_worst_first():
    pods = kube_cluster.parse_pods(load("pods_broken"), NOW)
    problems, _, _ = kube_cluster.split_pods(pods)
    names = [p["name"] for p in problems]

    # Waiting-reason pods (with most restarts first), then Failed/Unknown, then the rest.
    assert names[0] == "web-5d9f7c8b6-crash1"
    assert names.index("old-6f8d7b5c4-evicted") > names.index("worker-6b7d8c9f5-cfg1")


def test_restarts_only_count_when_recent():
    def pod(restarts, finished):
        return {
            "metadata": {"name": "p", "namespace": "n"},
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "ready": True,
                        "restartCount": restarts,
                        "state": {"running": {}},
                        "lastState": {"terminated": {"reason": "Error", "finishedAt": finished}},
                    }
                ],
            },
        }

    def problem_count(item):
        parsed = kube_cluster.parse_pods({"items": [item]}, NOW)
        return kube_cluster.split_pods(parsed)[2]["problem"]

    assert problem_count(pod(6, "2026-10-29T10:00:00Z")) == 1  # many restarts, recent
    assert problem_count(pod(6, "2026-10-20T10:00:00Z")) == 0  # many restarts, long ago
    assert problem_count(pod(1, "2026-10-29T10:00:00Z")) == 0  # one restart is normal


def test_not_ready_running_pod_is_a_problem():
    item = {
        "metadata": {"name": "p", "namespace": "n"},
        "spec": {"containers": [{"name": "a"}, {"name": "b"}]},
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {"ready": True, "restartCount": 0},
                {"ready": False, "restartCount": 0},
            ],
        },
    }
    parsed = kube_cluster.parse_pods({"items": [item]}, NOW)
    assert parsed[0]["ready"] == [1, 2]
    assert kube_cluster.split_pods(parsed)[2]["problem"] == 1


def test_ok_pods_are_grouped_by_namespace_and_capped():
    items = [
        {"metadata": {"name": f"p{i}", "namespace": f"ns{i % 3}"}, "status": {"phase": "Running"}}
        for i in range(30)
    ]
    pods = kube_cluster.parse_pods({"items": items}, NOW)

    _, ok, counts = kube_cluster.split_pods(pods, max_ok=10)

    assert counts["total"] == 30
    assert sum(len(v) for v in ok.values()) == 10


# --- workloads, helm, argo -----------------------------------------------------------------------


def test_parse_workloads_deployments_fixture():
    workloads = kube_cluster.parse_workloads("Deployment", load("deployments"))

    assert len(workloads) == 11
    server = by_name(workloads, "argocd-server")
    assert server == {
        "kind": "Deployment",
        "ns": "argocd",
        "name": "argocd-server",
        "desired": 1,
        "ready": 1,
        "available": 1,
        "updated": 1,
        "misscheduled": 0,
        "images": ["quay.io/argoproj/argocd:v3.5.6"],
        "helm": "argocd",
    }
    assert by_name(workloads, "coredns")["helm"] is None
    assert by_name(workloads, "traefik")["helm"] == "traefik"
    app = by_name(workloads, "example-app")
    assert app["ns"] == "apps"
    assert app["images"] == ["ghcr.io/example/example-app:sha-079b616"]  # de-duplicated


def test_parse_workloads_statefulset_and_daemonset_fixtures():
    [sts] = kube_cluster.parse_workloads("StatefulSet", load("statefulsets"))
    assert (sts["kind"], sts["desired"], sts["ready"], sts["available"]) == (
        "StatefulSet",
        1,
        1,
        1,
    )

    [ds] = kube_cluster.parse_workloads("DaemonSet", load("daemonsets"))
    assert (ds["kind"], ds["desired"], ds["ready"], ds["available"], ds["updated"]) == (
        "DaemonSet",
        3,
        3,
        3,
        3,
    )
    assert ds["misscheduled"] == 0


def test_daemonset_misscheduled_and_missing_status():
    item = {
        "metadata": {"name": "d", "namespace": "n"},
        "status": {"desiredNumberScheduled": 3, "numberMisscheduled": 2},
    }
    [ds] = kube_cluster.parse_workloads("DaemonSet", {"items": [item]})
    assert (ds["desired"], ds["ready"], ds["available"], ds["misscheduled"]) == (3, 0, 0, 2)


def test_deployment_without_status_counts_as_zero_available():
    item = {"metadata": {"name": "d", "namespace": "n"}, "spec": {"replicas": 3}}
    [dep] = kube_cluster.parse_workloads("Deployment", {"items": [item]})
    assert (dep["desired"], dep["ready"], dep["available"], dep["updated"]) == (3, 0, 0, 0)


def test_unknown_workload_kind_gives_nothing():
    assert kube_cluster.parse_workloads("CronJob", load("deployments")) == []


def test_workload_images_capped_and_only_plain_references():
    containers = [{"image": f"ghcr.io/example/img{i}:1"} for i in range(15)]
    containers += [
        {"image": "user:pass@evil/img"},  # credentials-looking: dropped
        {"image": "x" * 400},
        {"image": 42},
    ]
    item = {
        "metadata": {"name": "d", "namespace": "n"},
        "spec": {"template": {"spec": {"containers": containers}}},
    }
    [dep] = kube_cluster.parse_workloads("Deployment", {"items": [item]})
    assert len(dep["images"]) == 10
    assert all("pass" not in image for image in dep["images"])


def test_unpinned_image_fixture_is_parsed_faithfully():
    workloads = kube_cluster.parse_workloads("Deployment", load("deployments_unpinned"))

    assert by_name(workloads, "latest-app")["images"] == ["docker.io/library/nginx:latest"]
    assert by_name(workloads, "untagged-app")["images"] == ["docker.io/library/busybox"]
    pinned = by_name(workloads, "pinned-app")
    assert pinned["images"] == ["ghcr.io/example/pinned@sha256:" + "ab" * 32]
    assert (pinned["desired"], pinned["available"]) == (2, 0)


def test_helm_from_workloads_uses_labels_and_annotations():
    items = load("deployments")["items"] + load("statefulsets")["items"]

    releases = kube_cluster.helm_from_workloads(items)

    assert [r["name"] for r in releases] == ["argocd", "traefik"]  # one entry per release
    argocd = by_name(releases, "argocd")
    assert argocd == {
        "name": "argocd",
        "ns": "argocd",
        "chart": "argo-cd-10.11.6",
        "app_version": "v3.5.6",
        "source": "labels",
    }
    assert by_name(releases, "traefik")["chart"] == "traefik-39.0.701_up39.0.7"


def test_helm_from_workloads_without_annotations_uses_instance_label():
    item = {
        "metadata": {
            "name": "web",
            "namespace": "apps",
            "labels": {
                "app.kubernetes.io/managed-by": "Helm",
                "app.kubernetes.io/instance": "shop",
            },
        }
    }
    [release] = kube_cluster.helm_from_workloads([item])
    assert (release["name"], release["ns"], release["chart"]) == ("shop", "apps", None)
    # Not managed by Helm: not a release.
    item["metadata"]["labels"]["app.kubernetes.io/managed-by"] = "kustomize"
    assert kube_cluster.helm_from_workloads([item]) == []


def test_parse_helmcharts_fixture_keeps_only_name_chart_and_namespace():
    charts = kube_cluster.parse_helmcharts(load("helmcharts"))

    assert charts == [
        {
            "name": "traefik",
            "ns": "kube-system",
            "chart": "traefik-39.0.701+up39.0.7",
            "app_version": None,
            "source": "helmchart",
        },
        {
            "name": "traefik-crd",
            "ns": "kube-system",
            "chart": "traefik-crd-39.0.701+up39.0.7",
            "app_version": None,
            "source": "helmchart",
        },
    ]


def test_helmchart_version_and_target_namespace():
    item = {
        "metadata": {"name": "nginx", "namespace": "kube-system"},
        "spec": {"chart": "nginx", "version": "15.1.0", "targetNamespace": "web"},
    }
    [chart] = kube_cluster.parse_helmcharts({"items": [item]})
    assert (chart["ns"], chart["chart"]) == ("web", "nginx-15.1.0")


def test_parse_argo_apps_fixture():
    [app] = kube_cluster.parse_argo_apps(load("argo_applications"))

    assert app == {
        "name": "example-app",
        "ns": "argocd",
        "project": "default",
        "sync": "Synced",
        "health": "Healthy",
        "rev": "842ebc5",
        "op": "Succeeded",
        "synced_at": "2026-10-27T12:53:52Z",
    }


def test_argo_unknown_values_become_unknown():
    item = {
        "metadata": {"name": "a", "namespace": "argocd"},
        "status": {
            "sync": {"status": "<script>"},
            "health": {"status": "Whatever"},
            "operationState": {"phase": "Exploded", "finishedAt": "yesterday"},
        },
    }
    [app] = kube_cluster.parse_argo_apps({"items": [item]})
    assert (app["sync"], app["health"], app["op"], app["synced_at"]) == (
        "Unknown",
        "Unknown",
        None,
        None,
    )


def test_argocd_version_from_the_argocd_server_image():
    workloads = kube_cluster.parse_workloads("Deployment", load("deployments"))
    assert kube_cluster.argocd_version(workloads) == "v3.5.6"
    assert kube_cluster.argocd_version([]) is None
    digest_only = [
        {"name": "argocd-server", "images": ["quay.io/argoproj/argocd@sha256:" + "a" * 64]}
    ]
    assert kube_cluster.argocd_version(digest_only) is None


# --- pvcs and events ----------------------------------------------------------------------------


def test_parse_pvcs_fixture():
    [pvc] = kube_cluster.parse_pvcs(load("pvcs"), LAB_NOW)
    assert pvc == {
        "ns": "apps",
        "name": "example-app-data",
        "phase": "Bound",
        "class": "local-path",
        "size": "1Gi",
        "age_s": int((LAB_NOW - datetime(2026, 10, 26, 16, 30, 24, tzinfo=UTC)).total_seconds()),
    }


def test_pvcs_not_bound_sort_first():
    items = [
        {"metadata": {"name": "a", "namespace": "n"}, "status": {"phase": "Bound"}},
        {"metadata": {"name": "b", "namespace": "n"}, "status": {"phase": "Pending"}},
    ]
    assert [p["name"] for p in kube_cluster.parse_pvcs({"items": items}, NOW)] == ["b", "a"]


def test_parse_warning_events_fixture():
    events = kube_cluster.parse_warning_events(load("events_warning"), LAB_NOW)

    assert 0 < len(events) <= 20
    for event in events:
        assert set(event) == {"ns", "obj", "reason", "msg", "count", "age_s"}
        assert "/" in event["obj"]
        assert len(event["msg"]) <= 256
    ages = [e["age_s"] for e in events]
    assert ages == sorted(ages)  # newest (smallest age) first


def test_events_are_capped_to_the_newest_twenty_and_messages_are_cut():
    items = [
        {
            "metadata": {"name": f"e{i}", "namespace": "n"},
            "type": "Warning",
            "reason": "BackOff",
            "message": "m" * 1000 + "\x1b[31mred\x00",
            "count": i + 1,
            "lastTimestamp": f"2026-10-29T11:{i:02d}:00Z",
            "involvedObject": {"kind": "Pod", "name": f"p{i}"},
        }
        for i in range(30)
    ]

    events = kube_cluster.parse_warning_events({"items": items}, NOW)

    assert len(events) == 20
    assert events[0]["obj"] == "Pod/p29"
    assert all(len(e["msg"]) == 256 for e in events)
    assert all("\x1b" not in e["msg"] and "\x00" not in e["msg"] for e in events)


def test_events_that_are_not_warnings_are_skipped():
    item = {
        "metadata": {"name": "e", "namespace": "n"},
        "type": "Normal",
        "reason": "Started",
        "involvedObject": {"kind": "Pod", "name": "p"},
    }
    assert kube_cluster.parse_warning_events({"items": [item]}, NOW) == []


def test_event_without_count_or_time():
    item = {
        "metadata": {"name": "e", "namespace": "n"},
        "type": "Warning",
        "reason": "Failed",
        "involvedObject": {"kind": "Node", "name": "worker-1"},
    }
    [event] = kube_cluster.parse_warning_events({"items": [item]}, NOW)
    assert event["count"] == 1
    assert event["age_s"] is None


# --- secrets never come through ------------------------------------------------------------------

PARSE_OUTPUTS = [
    lambda: kube_cluster.parse_namespace_uid(load("namespace_kube_system")),
    lambda: kube_cluster.parse_version(load("version")),
    lambda: kube_cluster.parse_nodes(load("nodes")),
    lambda: kube_cluster.parse_node_metrics(load("node_metrics")),
    lambda: kube_cluster.parse_pods(load("pods"), LAB_NOW),
    lambda: kube_cluster.split_pods(kube_cluster.parse_pods(load("pods"), LAB_NOW)),
    lambda: kube_cluster.parse_workloads("Deployment", load("deployments")),
    lambda: kube_cluster.parse_workloads("StatefulSet", load("statefulsets")),
    lambda: kube_cluster.parse_workloads("DaemonSet", load("daemonsets")),
    lambda: kube_cluster.helm_from_workloads(
        load("deployments")["items"] + load("statefulsets")["items"] + load("daemonsets")["items"]
    ),
    lambda: kube_cluster.parse_helmcharts(load("helmcharts")),
    lambda: kube_cluster.parse_argo_apps(load("argo_applications")),
    lambda: kube_cluster.parse_pvcs(load("pvcs"), LAB_NOW),
    lambda: kube_cluster.parse_warning_events(load("events_warning"), LAB_NOW),
]


def test_the_fixtures_really_contain_the_sentinels():
    # If this fails the sanitizer lost them and the tests below prove nothing.
    text = "".join(path.read_text() for path in FIXTURES.glob("*.json"))
    for n in range(1, 10):
        assert f"SENTINEL-SECRET-{n}" in text


@pytest.mark.parametrize("make", PARSE_OUTPUTS, ids=range(len(PARSE_OUTPUTS)))
def test_no_parser_output_contains_a_sentinel(make):
    assert "SENTINEL" not in json.dumps(make())


def test_cluster_view_contains_no_sentinel():
    view = kube_cluster.collect_cluster_view(lab_client(), LAB_NOW)
    assert "SENTINEL" not in json.dumps(view)


def test_no_env_names_or_values_in_pods():
    pods = kube_cluster.parse_pods(load("pods"), LAB_NOW)
    text = json.dumps(pods)
    assert "DB_PASSWORD" not in text
    assert "example-value" not in text


# --- empty and garbage input: parsers never raise ----------------------------------------------

DEEP: Any = {}
for _ in range(2000):
    DEEP = {"items": [DEEP], "metadata": DEEP}

GARBAGE = [
    None,
    "",
    "text",
    42,
    3.5,
    True,
    [],
    [1, 2, 3],
    {},
    {"items": None},
    {"items": "x"},
    {"items": {"a": 1}},
    {"items": [None, 1, "x", [], {}]},
    {"items": [{"metadata": 5, "spec": "x", "status": []}]},
    {"items": [{"metadata": {"name": {"a": 1}, "namespace": [1]}}]},
    {"items": [{"metadata": {"name": "n", "namespace": "ns", "labels": [], "annotations": 5}}]},
    {"items": [{"metadata": {"name": "n", "ownerReferences": [None, 3, {"kind": 4}]}}]},
    {
        "items": [
            {"metadata": {"name": "n"}, "status": {"containerStatuses": [None, "x", {"state": 5}]}}
        ]
    },
    {"items": [{"metadata": {"name": "n", "creationTimestamp": "not a date"}}]},
    {"items": [{"metadata": {"name": "n", "creationTimestamp": 12345}}]},
    {"items": [{"metadata": {"name": "n"}, "spec": {"template": {"spec": {"containers": "x"}}}}]},
    {"items": [{"metadata": {"name": "n"}, "status": {"capacity": {"cpu": [], "memory": {}}}}]},
    {"items": [{"metadata": {"name": "\x00\x1b[2J" * 100, "namespace": "n"}}]},
    {"metadata": {"uid": ["x"]}, "gitVersion": {"a": 1}},
    {"items": [{"metadata": {"name": "n"}, "involvedObject": 5, "type": "Warning"}]},
    DEEP,
]

SIMPLE_PARSERS = [
    kube_cluster.parse_namespace_uid,
    kube_cluster.parse_version,
    kube_cluster.parse_node_metrics,
    kube_cluster.parse_nodes,
    kube_cluster.parse_helmcharts,
    kube_cluster.parse_argo_apps,
    lambda d: kube_cluster.parse_workloads("Deployment", d),
    lambda d: kube_cluster.parse_workloads("DaemonSet", d),
    lambda d: kube_cluster.parse_pods(d, NOW),
    lambda d: kube_cluster.parse_pvcs(d, NOW),
    lambda d: kube_cluster.parse_warning_events(d, NOW),
    lambda d: kube_cluster.helm_from_workloads(d),
    lambda d: kube_cluster.argocd_version(d),
    lambda d: kube_cluster.owner_of(d),
    lambda d: kube_cluster.split_pods(d),
]


@pytest.mark.parametrize("parser", SIMPLE_PARSERS, ids=range(len(SIMPLE_PARSERS)))
@pytest.mark.parametrize("data", GARBAGE, ids=range(len(GARBAGE)))
def test_parsers_never_raise_and_stay_json_safe(parser, data):
    result = parser(data)
    json.dumps(result)


@pytest.mark.parametrize(
    ("parser", "empty"),
    [
        (kube_cluster.parse_nodes, []),
        (kube_cluster.parse_node_metrics, {}),
        (kube_cluster.parse_helmcharts, []),
        (kube_cluster.parse_argo_apps, []),
        (lambda d: kube_cluster.parse_pods(d, NOW), []),
        (lambda d: kube_cluster.parse_pvcs(d, NOW), []),
        (lambda d: kube_cluster.parse_warning_events(d, NOW), []),
    ],
)
@pytest.mark.parametrize("data", [None, "x", 5, [], {}, {"items": None}, {"items": [None, 1]}])
def test_empty_input_gives_the_empty_result(parser, empty, data):
    assert parser(data) == empty


def test_hostile_names_have_control_characters_removed():
    data = {"items": [{"metadata": {"name": "a\x1b[31mb\nc", "namespace": "n"}}]}
    [pod] = kube_cluster.parse_pods(data, NOW)
    assert all(ord(ch) >= 32 for ch in pod["name"])


# --- collect_cluster_view ------------------------------------------------------------------------

API = kube_api


class FakeClient:
    """Stands in for KubeClient: maps an API path to a list body, or to an exception."""

    def __init__(
        self, responses, *, sub="system:serviceaccount:monitoring:cronrack-agent", exp=None
    ):
        self.responses = responses
        self.sub = sub
        self.exp = exp
        self.calls: list[tuple[str, dict | None]] = []
        self.cert_days = 290
        self.truncated_paths: set[str] = set()

    def start(self):
        pass

    def token_details(self):
        return {"sub": self.sub, "exp": self.exp}

    def api_cert_days(self):
        return self.cert_days

    def _answer(self, path, query):
        self.calls.append((path, query))
        answer = self.responses.get(path, NotInstalled())
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get_json(self, path, query=None):
        return self._answer(path, query)

    def list_all(self, path, query=None):
        body = self._answer(path, query)
        return list(body["items"]), path in self.truncated_paths


def lab_responses():
    return {
        API.NAMESPACE.path: load("namespace_kube_system"),
        API.VERSION.path: load("version"),
        API.NODES.path: load("nodes"),
        API.NODE_METRICS.path: load("node_metrics"),
        API.PODS.path: load("pods"),
        API.DEPLOYMENTS.path: load("deployments"),
        API.STATEFULSETS.path: load("statefulsets"),
        API.DAEMONSETS.path: load("daemonsets"),
        API.PVCS.path: load("pvcs"),
        API.EVENTS.path: load("events_warning"),
        API.HELMCHARTS.path: load("helmcharts"),
        API.APPLICATIONS.path: load("argo_applications"),
    }


def lab_client(**overrides):
    return FakeClient({**lab_responses(), **overrides})


def test_collect_cluster_view_lab():
    exp = int(datetime(2027, 10, 29, tzinfo=UTC).timestamp())
    client = FakeClient(lab_responses(), exp=exp)

    view = kube_cluster.collect_cluster_view(client, LAB_NOW)

    assert set(view) == {
        "uid",
        "version",
        "api",
        "sa",
        "token_days",
        "denied",
        "missing",
        "nodes",
        "problem_pods",
        "ok_pods",
        "pod_counts",
        "workloads",
        "helm",
        "argo",
        "argocd_version",
        "pvcs",
        "events",
        "truncated",
        "cut",
    }
    assert view["uid"] == KUBE_SYSTEM_UID
    assert view["version"] == "v1.35.5+k3s1"
    assert view["api"] == {"error": None, "cert_days": 290}
    assert view["sa"] == "system:serviceaccount:monitoring:cronrack-agent"
    assert view["token_days"] == 365
    assert view["denied"] == []
    assert view["missing"] == []
    assert [n["name"] for n in view["nodes"]] == ["server-1", "worker-1", "worker-2"]
    assert by_name(view["nodes"], "worker-1")["cpu_used_m"] == 65
    assert view["pod_counts"]["total"] == 17
    assert view["pod_counts"]["succeeded"] == 2
    assert len(view["workloads"]) == 13
    assert {h["name"] for h in view["helm"]} == {"argocd", "traefik", "traefik-crd"}
    assert [a["name"] for a in view["argo"]] == ["example-app"]
    assert view["argocd_version"] == "v3.5.6"
    assert len(view["pvcs"]) == 1
    assert 0 < len(view["events"]) <= 20
    assert view["truncated"] is False
    assert view["cut"] == []
    json.dumps(view)


def test_helm_charts_already_listed_from_labels_are_not_duplicated():
    view = kube_cluster.collect_cluster_view(lab_client(), LAB_NOW)

    traefik = [h for h in view["helm"] if h["name"] == "traefik"]
    assert len(traefik) == 1
    assert traefik[0]["source"] == "labels"
    assert by_name(view["helm"], "traefik-crd")["source"] == "helmchart"


def test_events_are_requested_with_the_warning_selector_only():
    client = lab_client()
    kube_cluster.collect_cluster_view(client, LAB_NOW)

    queries = {path: query for path, query in client.calls}
    assert queries[API.EVENTS.path] == {"fieldSelector": "type=Warning"}


def test_only_known_paths_are_requested():
    client = lab_client()
    kube_cluster.collect_cluster_view(client, LAB_NOW)

    allowed = {r.path for r in API.ALL_RESOURCES}
    assert {path for path, _ in client.calls} <= allowed


def test_token_without_exp_has_no_token_days():
    view = kube_cluster.collect_cluster_view(FakeClient(lab_responses(), exp=None), LAB_NOW)
    assert view["token_days"] is None


def test_optional_apis_that_are_not_installed_are_listed_as_missing():
    client = lab_client(
        **{
            API.APPLICATIONS.path: NotInstalled(),
            API.HELMCHARTS.path: NotInstalled(),
            API.NODE_METRICS.path: NotInstalled(),
        }
    )

    view = kube_cluster.collect_cluster_view(client, LAB_NOW)

    assert view["missing"] == ["argoproj.io", "helm.cattle.io", "metrics.k8s.io"]
    assert view["argo"] == []
    assert by_name(view["nodes"], "worker-1")["cpu_used_m"] is None
    assert view["api"]["error"] is None


def test_denied_resources_are_recorded_and_the_rest_still_works():
    client = lab_client(
        **{
            API.PODS.path: Denied("", "pods", "list"),
            API.APPLICATIONS.path: Denied("argoproj.io", "applications", "list"),
        }
    )

    view = kube_cluster.collect_cluster_view(client, LAB_NOW)

    assert view["denied"] == [
        {"g": "", "r": "pods", "v": "list"},
        {"g": "argoproj.io", "r": "applications", "v": "list"},
    ]
    assert view["pod_counts"]["total"] == 0
    assert len(view["nodes"]) == 3
    assert view["api"]["error"] is None


def test_denied_namespace_means_no_uid_but_still_a_view():
    client = lab_client(**{API.NAMESPACE.path: Denied("", "namespaces", "get")})

    view = kube_cluster.collect_cluster_view(client, LAB_NOW)

    assert view["uid"] is None
    assert view["denied"] == [{"g": "", "r": "namespaces", "v": "get"}]
    assert len(view["nodes"]) == 3


def test_denied_entries_are_capped_and_unique():
    responses = {r.path: Denied(r.group, r.resource, r.verb) for r in API.ALL_RESOURCES}
    view = kube_cluster.collect_cluster_view(FakeClient(responses), LAB_NOW)

    assert len(view["denied"]) == len({tuple(d.values()) for d in view["denied"]})
    assert len(view["denied"]) <= 20


@pytest.mark.parametrize("kind", ["refused", "tls", "timeout", "bad_response"])
def test_unreachable_api_stops_early_and_reports_the_error(kind):
    responses = {API.NAMESPACE.path: ApiError(kind)}
    client = FakeClient(responses)

    view = kube_cluster.collect_cluster_view(client, LAB_NOW)

    assert view["api"]["error"] == kind
    assert view["nodes"] == []
    assert view["uid"] is None
    assert len(client.calls) == 1  # no point asking again


def test_unauthorized_stops_everything():
    client = lab_client(**{API.NODES.path: Unauthorized()})

    view = kube_cluster.collect_cluster_view(client, LAB_NOW)

    assert view["api"]["error"] == "unauthorized"
    assert view["nodes"] == []
    assert view["pod_counts"]["total"] == 0
    assert API.PODS.path not in [path for path, _ in client.calls]


def test_a_bad_response_for_one_list_does_not_hide_the_others():
    client = lab_client(**{API.PVCS.path: ApiError("bad_response")})

    view = kube_cluster.collect_cluster_view(client, LAB_NOW)

    assert view["api"]["error"] is None
    assert view["pvcs"] == []
    assert view["truncated"] is True
    assert "pvcs" in view["cut"]
    assert len(view["nodes"]) == 3


def test_deadline_marks_the_rest_as_cut():
    client = lab_client(**{API.PODS.path: kube_api.DeadlineExceeded()})

    view = kube_cluster.collect_cluster_view(client, LAB_NOW)

    assert view["truncated"] is True
    assert "pods" in view["cut"]
    assert view["api"]["error"] is None
    assert len(view["nodes"]) == 3


def test_token_problem_is_unauthorized():
    class NoToken(FakeClient):
        def start(self):
            raise kube_api.TokenError("cannot read token_file")

    view = kube_cluster.collect_cluster_view(NoToken(lab_responses()), LAB_NOW)

    assert view["api"]["error"] == "unauthorized"
    assert view["nodes"] == []


def test_client_truncation_is_reported():
    client = lab_client()
    client.truncated_paths = {API.PODS.path}

    view = kube_cluster.collect_cluster_view(client, LAB_NOW)

    assert view["truncated"] is True
    assert "pods" in view["cut"]


def many_pods(total, broken):
    items = []
    for i in range(total):
        item = {
            "metadata": {"name": f"pod-{i:04d}", "namespace": f"ns{i % 150}"},
            "spec": {"nodeName": "worker-1"},
            "status": {"phase": "Running"},
        }
        if i < broken:
            item["status"] = {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "ready": False,
                        "restartCount": 1,
                        "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                    }
                ],
            }
        items.append(item)
    return {"items": items}


def test_caps_keep_problem_pods_and_drop_healthy_ones_first():
    client = lab_client(**{API.PODS.path: many_pods(1000, broken=150)})

    view = kube_cluster.collect_cluster_view(client, LAB_NOW)

    assert view["pod_counts"]["total"] == 1000
    assert view["pod_counts"]["problem"] == 150
    assert len(view["problem_pods"]) == 100
    assert all(p["reason"] == "CrashLoopBackOff" for p in view["problem_pods"])
    assert sum(len(v) for v in view["ok_pods"].values()) <= 400
    assert len(view["ok_pods"]) <= 100
    assert view["truncated"] is True
    assert {"problem_pods", "ok_pods"} <= set(view["cut"])


def test_workloads_cap_puts_unhealthy_first():
    items = [
        {
            "metadata": {"name": f"w{i:03d}", "namespace": "n"},
            "spec": {"replicas": 1},
            "status": {"availableReplicas": 0 if i == 299 else 1, "readyReplicas": 1},
        }
        for i in range(300)
    ]
    client = lab_client(**{API.DEPLOYMENTS.path: {"items": items}})

    view = kube_cluster.collect_cluster_view(client, LAB_NOW)

    deployments = [w for w in view["workloads"] if w["kind"] == "Deployment"]
    assert len(view["workloads"]) == 200
    assert deployments[0]["name"] == "w299"  # the one unavailable workload survives the cut
    assert "workloads" in view["cut"]


def test_nodes_pvcs_helm_and_argo_caps():
    nodes = {"items": [{"metadata": {"name": f"n{i:03d}"}} for i in range(80)]}
    pvcs = {"items": [{"metadata": {"name": f"c{i:03d}", "namespace": "n"}} for i in range(150)]}
    apps = {
        "items": [{"metadata": {"name": f"a{i:03d}", "namespace": "argocd"}} for i in range(150)]
    }
    charts = {
        "items": [
            {"metadata": {"name": f"h{i:03d}", "namespace": "kube-system"}} for i in range(150)
        ]
    }
    client = lab_client(
        **{
            API.NODES.path: nodes,
            API.PVCS.path: pvcs,
            API.APPLICATIONS.path: apps,
            API.HELMCHARTS.path: charts,
        }
    )

    view = kube_cluster.collect_cluster_view(client, LAB_NOW)

    assert len(view["nodes"]) == 50
    assert len(view["pvcs"]) == 100
    assert len(view["argo"]) == 100
    assert len(view["helm"]) == 100
    assert {"nodes", "pvcs", "argo", "helm"} <= set(view["cut"])
    assert view["truncated"] is True


def test_argo_apps_out_of_sync_come_first():
    apps = {
        "items": [
            {
                "metadata": {"name": "a-fine", "namespace": "argocd"},
                "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
            },
            {
                "metadata": {"name": "z-broken", "namespace": "argocd"},
                "status": {"sync": {"status": "OutOfSync"}, "health": {"status": "Degraded"}},
            },
        ]
    }
    view = kube_cluster.collect_cluster_view(lab_client(**{API.APPLICATIONS.path: apps}), LAB_NOW)
    assert [a["name"] for a in view["argo"]] == ["z-broken", "a-fine"]


def test_collect_cluster_view_with_the_broken_fixture():
    client = lab_client(
        **{API.PODS.path: load("pods_broken"), API.DEPLOYMENTS.path: load("deployments_unpinned")}
    )

    view = kube_cluster.collect_cluster_view(client, NOW)

    assert len(view["problem_pods"]) == 7
    assert view["pod_counts"]["problem"] == 7
    assert view["ok_pods"] == {"apps": [["web-5d9f7c8b6-fine1", "worker-1", 0, 86400]]}
    # Unhealthy workloads sort before healthy ones.
    first_deployments = [w["name"] for w in view["workloads"] if w["kind"] == "Deployment"]
    assert first_deployments[0] == "pinned-app"


def test_collect_never_raises_on_garbage_responses():
    responses = {r.path: {"items": [None, 5, "x"]} for r in API.ALL_RESOURCES}
    responses[API.NAMESPACE.path] = ["not", "a", "dict"]
    responses[API.VERSION.path] = "oops"

    view = kube_cluster.collect_cluster_view(FakeClient(responses), LAB_NOW)

    assert view["uid"] is None
    assert view["version"] is None
    json.dumps(view)
