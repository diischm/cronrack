"""The `kubernetes` part of a report: strict, capped, and optional for older agents."""

import copy
import json
import re
from datetime import UTC, datetime

import pytest

from agent.config import AgentConfig, KubernetesConfig
from agent.kube_cluster import collect_cluster_view
from agent.report import build_report, fit_report
from agent.tests.test_kube_cluster import LAB_NOW, FakeClient, lab_responses
from agents import serializers as s
from agents.serializers import ReportSerializer

from .sample import SAMPLE_REPORT, make_cluster, make_kube_report, make_report

CLUSTER_UID_UPPER = "08528EE9-428D-5167-8A8E-DF19E635932F"


def check(report) -> ReportSerializer:
    serializer = ReportSerializer(data=report)
    serializer.is_valid()
    return serializer


def is_valid(report) -> bool:
    return ReportSerializer(data=report).is_valid()


# --- compatibility with older agents ------------------------------------------------------


def test_report_without_the_key_is_valid():
    assert "kubernetes" not in SAMPLE_REPORT
    assert is_valid(make_report())


def test_kubernetes_may_be_null_when_the_collector_crashed():
    assert is_valid(make_report(kubernetes=None))


def test_sample_kubernetes_report_round_trips_unchanged():
    report = make_kube_report()
    serializer = check(report)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == report


def test_node_only_report_is_valid():
    assert is_valid(make_kube_report(cluster=None, role="agent", k3s="v1.31.2+k3s1"))


# --- contract with the real agent ---------------------------------------------------------------


def enabled_config() -> AgentConfig:
    return AgentConfig(kubernetes=KubernetesConfig(enabled=True, cluster_view=True))


NODE = {
    "role": "server",
    "dist": "k3s",
    "name": "server-1",
    "k3s": "v1.35.5+k3s1",
    "kubelet": None,
    "helm": "v3.16.2",
}


def real_report(client) -> dict:
    """The agent's own build_report() with the cluster read from the lab fixtures."""
    return build_report(
        enabled_config(),
        now=datetime(2026, 10, 29, 0, 0, 0, tzinfo=UTC),
        collectors={
            "kubernetes": lambda: {"node": NODE, "cluster": collect_cluster_view(client, LAB_NOW)},
            **dict.fromkeys(["updates", "reboot", "containers", "units", "timers", "disks"], None),
        },
    )


def test_real_agent_report_with_kubernetes_is_valid():
    """Contract test: what the agent really emits from the lab fixtures passes unchanged."""
    report = real_report(FakeClient(lab_responses()))
    assert report["kubernetes"]["cluster"]["workloads"]

    serializer = check(report)

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == report


def test_real_agent_report_when_the_api_is_unreachable_is_valid():
    from agent.kube_api import ApiError

    report = real_report(FakeClient({"/api/v1/namespaces/kube-system": ApiError("refused")}))

    assert report["kubernetes"]["cluster"]["api"]["error"] == "refused"
    assert is_valid(report)


def test_real_agent_report_with_denied_and_missing_apis_is_valid():
    from agent import kube_api
    from agent.kube_api import Denied, NotInstalled

    client = FakeClient(
        {
            **lab_responses(),
            kube_api.NAMESPACE.path: Denied("", "namespaces", "get"),
            kube_api.PODS.path: Denied("", "pods", "list"),
            kube_api.APPLICATIONS.path: NotInstalled(),
        }
    )
    report = real_report(client)

    assert report["kubernetes"]["cluster"]["uid"] is None
    assert report["kubernetes"]["cluster"]["denied"]
    assert report["kubernetes"]["cluster"]["missing"] == ["argoproj.io"]
    assert is_valid(report)


def test_every_step_of_fit_report_stays_valid():
    report = real_report(FakeClient(lab_responses()))
    sizes = []
    for limit in (10**9, 6000, 3000, 1500, 100):
        fitted = fit_report(report, limit)
        sizes.append(fitted["kubernetes"]["cluster"] is None)
        assert is_valid(fitted), limit
    assert sizes[-1] is True  # the last resort: no cluster at all


# --- strictness ---------------------------------------------------------------------------------


def with_cluster(**changes):
    return make_kube_report(cluster=make_cluster(**changes))


def test_unknown_keys_are_rejected_at_every_level():
    report = make_kube_report()
    report["kubernetes"]["extra"] = 1
    assert not is_valid(report)

    report = make_kube_report()
    report["kubernetes"]["node"]["extra"] = 1
    assert not is_valid(report)

    report = make_kube_report()
    report["kubernetes"]["cluster"]["nodes"][0]["extra"] = 1
    assert not is_valid(report)

    report = make_kube_report()
    report["kubernetes"]["cluster"]["api"]["extra"] = 1
    assert not is_valid(report)


@pytest.mark.parametrize("key", list(make_cluster()))
def test_every_cluster_key_is_required(key):
    cluster = make_cluster()
    del cluster[key]
    assert not is_valid(make_kube_report(cluster=cluster))


@pytest.mark.parametrize("key", ["role", "dist", "name", "k3s", "kubelet", "helm"])
def test_every_node_key_is_required(key):
    report = make_kube_report()
    del report["kubernetes"]["node"][key]
    assert not is_valid(report)


def test_node_and_cluster_keys_are_required_in_kubernetes():
    report = make_kube_report()
    del report["kubernetes"]["cluster"]
    assert not is_valid(report)
    report = make_kube_report()
    del report["kubernetes"]["node"]
    assert not is_valid(report)


@pytest.mark.parametrize(
    ("change", "valid"),
    [
        ({"role": "server"}, True),
        ({"role": "agent", "dist": "k3s"}, True),
        ({"role": "node", "dist": "kubernetes"}, True),
        ({"role": "none", "dist": None}, True),
        ({"role": "master"}, False),
        ({"role": None}, False),
        ({"dist": "rke2"}, False),
        ({"name": "x" * 63}, True),
        ({"name": "x" * 64}, False),
        ({"name": "bad\nname"}, False),
        ({"k3s": "v1.31.2+k3s1"}, True),
        ({"k3s": "1.31.2"}, False),
        ({"k3s": "v1.31.2; rm -rf /"}, False),
        ({"kubelet": "v1.31.2-rc.1"}, True),
        ({"helm": "v3.16.2"}, True),
        ({"helm": 3}, False),
    ],
)
def test_node_part_values(change, valid):
    report = make_kube_report(**change)
    assert is_valid(report) is valid


@pytest.mark.parametrize(
    ("change", "valid"),
    [
        ({"uid": None}, True),
        ({"uid": "not-a-uuid"}, False),
        ({"uid": CLUSTER_UID_UPPER}, False),
        ({"version": "v1.31.2+k3s1"}, True),
        ({"version": None}, True),
        ({"version": "garbage"}, False),
        ({"api": {"error": "tls", "cert_days": 5}}, True),
        ({"api": {"error": "refused", "cert_days": None}}, True),
        ({"api": {"error": "timeout", "cert_days": None}}, True),
        ({"api": {"error": "unauthorized", "cert_days": None}}, True),
        ({"api": {"error": "bad_response", "cert_days": -3}}, True),
        ({"api": {"error": "exploded", "cert_days": 1}}, False),
        ({"api": {"error": None, "cert_days": "5"}}, False),
        ({"api": {"error": None, "cert_days": True}}, False),
        ({"sa": "system:serviceaccount:monitoring:cronrack-agent"}, True),
        ({"sa": None}, True),
        ({"sa": "system serviceaccount"}, False),
        ({"token_days": -1}, True),
        ({"token_days": 2.5}, False),
        ({"truncated": 1}, False),
        ({"truncated": True, "cut": ["ok_pods", "events"]}, True),
        ({"cut": ["Not A Name"]}, False),
        ({"cut": ["pods"] * 21}, False),
        ({"incomplete": ["pods", "workloads"]}, True),
        ({"incomplete": ["Not A Name"]}, False),
        ({"incomplete": ["pods"] * 21}, False),
        ({"incomplete": "pods"}, False),
        ({"missing": ["argoproj.io"]}, True),
        ({"missing": [5]}, False),
        ({"argocd_version": "v3.5.6"}, True),
        ({"argocd_version": "2.13.1"}, True),
        ({"argocd_version": "latest"}, False),
    ],
)
def test_cluster_scalar_values(change, valid):
    assert is_valid(with_cluster(**change)) is valid


def test_denied_entries():
    ok = {"g": "", "r": "pods", "v": "list"}
    assert is_valid(with_cluster(denied=[ok]))
    assert not is_valid(with_cluster(denied=[{**ok, "extra": 1}]))
    assert not is_valid(with_cluster(denied=[{"g": "", "r": "pods"}]))
    assert is_valid(with_cluster(denied=[ok] * s.K8S_MAX_DENIED))
    assert not is_valid(with_cluster(denied=[ok] * (s.K8S_MAX_DENIED + 1)))


NODE_ITEM = make_cluster()["nodes"][0]
POD_ITEM = {
    "ns": "apps",
    "name": "web-5d9f7c8b6-x2k4q",
    "node": "worker-1",
    "phase": "Running",
    "ready": [0, 1],
    "restarts": 12,
    "reason": "CrashLoopBackOff",
    "last": "OOMKilled",
    "last_age_s": 120,
    "age_s": 86400,
    "owner": "Deployment/web",
}
WORKLOAD_ITEM = make_cluster()["workloads"][0]
HELM_ITEM = {
    "name": "web",
    "ns": "apps",
    "chart": "web-1.4.2",
    "app_version": "1.4.2",
    "source": "labels",
}
ARGO_ITEM = {
    "name": "web",
    "ns": "argocd",
    "project": "default",
    "sync": "Synced",
    "health": "Healthy",
    "rev": "a1b2c3d",
    "op": "Succeeded",
    "synced_at": "2026-10-28T10:00:00Z",
}
PVC_ITEM = {
    "ns": "apps",
    "name": "data-web-0",
    "phase": "Bound",
    "class": "local-path",
    "size": "1Gi",
    "age_s": 9000,
}
EVENT_ITEM = {
    "ns": "apps",
    "obj": "Pod/web-5d9f7c8b6-x2k4q",
    "reason": "BackOff",
    "msg": "Back-off restarting failed container web",
    "count": 12,
    "age_s": 300,
}

LISTS = [
    ("nodes", NODE_ITEM, s.K8S_MAX_NODES),
    ("problem_pods", POD_ITEM, s.K8S_MAX_PROBLEM_PODS),
    ("workloads", WORKLOAD_ITEM, s.K8S_MAX_WORKLOADS),
    ("helm", HELM_ITEM, s.K8S_MAX_HELM),
    ("argo", ARGO_ITEM, s.K8S_MAX_ARGO),
    ("pvcs", PVC_ITEM, s.K8S_MAX_PVCS),
    ("events", EVENT_ITEM, s.K8S_MAX_EVENTS),
]


@pytest.mark.parametrize(("key", "item", "cap"), LISTS, ids=[x[0] for x in LISTS])
def test_each_list_accepts_its_cap_and_refuses_one_more(key, item, cap):
    assert is_valid(with_cluster(**{key: [item]}))
    assert is_valid(with_cluster(**{key: [item] * cap}))
    assert not is_valid(with_cluster(**{key: [item] * (cap + 1)}))


@pytest.mark.parametrize(("key", "item", "cap"), LISTS, ids=[x[0] for x in LISTS])
def test_each_list_item_rejects_unknown_keys_and_wrong_types(key, item, cap):
    assert not is_valid(with_cluster(**{key: [{**item, "extra": 1}]}))
    assert not is_valid(with_cluster(**{key: ["x"]}))
    assert not is_valid(with_cluster(**{key: {}}))


def mutate(item, **changes):
    return {**item, **changes}


@pytest.mark.parametrize(
    ("key", "item", "change", "valid"),
    [
        ("nodes", NODE_ITEM, {"pressure": ["MemoryPressure", "DiskPressure"]}, True),
        ("nodes", NODE_ITEM, {"pressure": ["Hot"]}, False),
        ("nodes", NODE_ITEM, {"ready": "True"}, False),
        ("nodes", NODE_ITEM, {"roles": ["a"] * 6}, False),
        ("nodes", NODE_ITEM, {"cpu_m": -1}, False),
        ("nodes", NODE_ITEM, {"cpu_m": None, "mem_mi": None}, True),
        ("nodes", NODE_ITEM, {"name": "n" * 253}, True),
        ("nodes", NODE_ITEM, {"os": "x" * 33}, False),
        ("problem_pods", POD_ITEM, {"phase": "Pending", "node": None, "reason": None}, True),
        ("problem_pods", POD_ITEM, {"phase": "Crashed"}, False),
        ("problem_pods", POD_ITEM, {"reason": "Unschedulable"}, True),
        ("problem_pods", POD_ITEM, {"reason": "bad reason"}, False),
        ("problem_pods", POD_ITEM, {"ready": [1]}, False),
        ("problem_pods", POD_ITEM, {"ready": [1, 2, 3]}, False),
        ("problem_pods", POD_ITEM, {"ready": ["1", "1"]}, False),
        ("problem_pods", POD_ITEM, {"restarts": 1_000_001}, False),
        ("problem_pods", POD_ITEM, {"restarts": -1}, False),
        ("problem_pods", POD_ITEM, {"age_s": 315_360_001}, False),
        ("problem_pods", POD_ITEM, {"age_s": None, "last_age_s": None, "last": None}, True),
        ("problem_pods", POD_ITEM, {"name": 5}, False),  # not a string: still refused
        ("problem_pods", POD_ITEM, {"owner": None}, False),
        ("workloads", WORKLOAD_ITEM, {"kind": "Job"}, False),
        ("workloads", WORKLOAD_ITEM, {"kind": "DaemonSet", "misscheduled": 2}, True),
        ("workloads", WORKLOAD_ITEM, {"misscheduled": None}, False),
        ("workloads", WORKLOAD_ITEM, {"images": ["ghcr.io/example/web:1.4.2"] * 10}, True),
        ("workloads", WORKLOAD_ITEM, {"images": ["ghcr.io/example/web:1.4.2"] * 11}, False),
        ("workloads", WORKLOAD_ITEM, {"images": ["nginx@sha256:" + "a" * 64]}, True),
        ("workloads", WORKLOAD_ITEM, {"images": ["bad image"]}, False),
        ("workloads", WORKLOAD_ITEM, {"images": ["x" * 400]}, False),
        ("workloads", WORKLOAD_ITEM, {"helm": "web"}, True),
        ("helm", HELM_ITEM, {"source": "helmchart", "chart": None, "app_version": None}, True),
        ("helm", HELM_ITEM, {"source": "repo"}, False),
        ("helm", HELM_ITEM, {"chart": "c" * 121}, False),
        ("argo", ARGO_ITEM, {"sync": "OutOfSync", "health": "Degraded", "op": "Failed"}, True),
        ("argo", ARGO_ITEM, {"sync": "Broken"}, False),
        ("argo", ARGO_ITEM, {"health": "Great"}, False),
        ("argo", ARGO_ITEM, {"op": "Sleeping"}, False),
        ("argo", ARGO_ITEM, {"op": None, "rev": None, "synced_at": None, "project": None}, True),
        ("argo", ARGO_ITEM, {"rev": "a1b2c3d4"}, False),
        ("argo", ARGO_ITEM, {"synced_at": "yesterday"}, False),
        ("pvcs", PVC_ITEM, {"phase": "Pending", "class": None, "size": None, "age_s": None}, True),
        ("pvcs", PVC_ITEM, {"phase": "Full"}, False),
        ("pvcs", PVC_ITEM, {"size": "1 Gi"}, False),
        ("pvcs", PVC_ITEM, {"class": 5}, False),
        ("events", EVENT_ITEM, {"msg": "m" * 256}, True),
        ("events", EVENT_ITEM, {"msg": "m" * 257}, False),
        ("events", EVENT_ITEM, {"msg": ""}, True),
        ("events", EVENT_ITEM, {"msg": "line\nbreak"}, False),
        ("events", EVENT_ITEM, {"reason": None, "age_s": None}, True),
        ("events", EVENT_ITEM, {"count": -1}, False),
    ],
)
def test_list_item_values(key, item, change, valid):
    assert is_valid(with_cluster(**{key: [mutate(item, **change)]})) is valid


# --- Kubernetes names (DNS-1123) -----------------------------------------------------------------
#
# A string that isn't a valid name is replaced by INVALID_K8S_NAME: the pod or app is still
# shown (it may be the broken one), the odd name isn't. Only the field is refused.

BAD_NAMES = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "Web",  # upper case
    "web_1",
    "-web",
    "web-",
    "",
    "web\x1b[31m",
    "pod\u0085",
    "n" * 254,
    "a/b",
    "javascript:alert(1)",
]
LABEL_BAD = [*BAD_NAMES, "a.b", "n" * 64]  # namespaces are labels: no dots, at most 63

NAME_FIELDS = [
    ("nodes", NODE_ITEM, "name", BAD_NAMES),
    ("problem_pods", POD_ITEM, "ns", LABEL_BAD),
    ("problem_pods", POD_ITEM, "name", BAD_NAMES),
    ("problem_pods", POD_ITEM, "node", BAD_NAMES),
    ("workloads", WORKLOAD_ITEM, "ns", LABEL_BAD),
    ("workloads", WORKLOAD_ITEM, "name", BAD_NAMES),
    ("workloads", WORKLOAD_ITEM, "helm", BAD_NAMES),
    ("helm", HELM_ITEM, "name", BAD_NAMES),
    ("helm", HELM_ITEM, "ns", LABEL_BAD),
    ("argo", ARGO_ITEM, "name", BAD_NAMES),
    ("argo", ARGO_ITEM, "ns", LABEL_BAD),
    ("argo", ARGO_ITEM, "project", BAD_NAMES),
    ("pvcs", PVC_ITEM, "ns", LABEL_BAD),
    ("pvcs", PVC_ITEM, "name", BAD_NAMES),
    ("pvcs", PVC_ITEM, "class", BAD_NAMES),
    ("events", EVENT_ITEM, "ns", LABEL_BAD),
]


def validated_item(key, item):
    serializer = check(with_cluster(**{key: [item]}))
    assert serializer.is_valid(), serializer.errors
    return serializer.validated_data["kubernetes"]["cluster"][key][0]


@pytest.mark.parametrize(
    ("key", "item", "field", "bad"),
    [(k, i, f, b) for k, i, f, bads in NAME_FIELDS for b in bads],
    ids=lambda v: v if isinstance(v, str) and len(v) < 30 else None,
)
def test_a_bad_kubernetes_name_is_replaced_not_refused(key, item, field, bad):
    value = validated_item(key, {**item, field: bad})
    assert value[field] == s.INVALID_K8S_NAME
    assert value != {**item, field: bad}  # the rest of the item survives
    assert {k: v for k, v in value.items() if k != field} == {
        k: v for k, v in item.items() if k != field
    }


@pytest.mark.parametrize(
    "good",
    ["web", "web-5d9f7c8b6-x2k4q", "a", "0", "kube-system", "n" * 253, "data.web-0", "a1.b2.c3"],
)
def test_valid_names_pass_unchanged(good):
    assert validated_item("problem_pods", {**POD_ITEM, "name": good})["name"] == good
    assert validated_item("nodes", {**NODE_ITEM, "name": good})["name"] == good


@pytest.mark.parametrize("good", ["apps", "kube-system", "n" * 63, "a"])
def test_valid_namespaces_pass_unchanged(good):
    assert validated_item("problem_pods", {**POD_ITEM, "ns": good})["ns"] == good


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        ("Deployment/web", True),
        ("StatefulSet/data-web", True),
        ("Pod/unknown", True),
        ("Node/server-1", True),
        ("web", False),  # a reference needs Kind/name
        ("Deployment/web/extra", False),
        ("deployment/web", False),
        ("Deployment/<b>web</b>", False),
        ("Deploy ment/web", False),
        ("D" * 64 + "/web", False),
        ("Deployment/" + "n" * 254, False),
    ],
)
def test_object_references_allow_one_slash(value, kept):
    expected = value if kept else s.INVALID_K8S_NAME
    assert validated_item("problem_pods", {**POD_ITEM, "owner": value})["owner"] == expected
    assert validated_item("events", {**EVENT_ITEM, "obj": value})["obj"] == expected


def test_null_stays_null_where_a_name_is_optional():
    assert validated_item("problem_pods", {**POD_ITEM, "node": None})["node"] is None
    assert validated_item("pvcs", {**PVC_ITEM, "class": None})["class"] is None


def test_bad_names_in_ok_pods_are_replaced_and_namespaces_merge():
    report = with_cluster(
        ok_pods={
            "apps": [["web-1", "worker-1", 0, 5], ["<b>x</b>", "Worker 1", 0, 5]],
            "Bad NS": [["db-0", None, 1, 7]],
            "also bad\x1b": [["db-1", None, 1, 7]],
        }
    )
    serializer = check(report)
    assert serializer.is_valid(), serializer.errors
    ok_pods = serializer.validated_data["kubernetes"]["cluster"]["ok_pods"]
    bad = s.INVALID_K8S_NAME
    assert ok_pods == {
        "apps": [["web-1", "worker-1", 0, 5], [bad, bad, 0, 5]],
        bad: [["db-0", None, 1, 7], ["db-1", None, 1, 7]],  # both bad namespaces, none lost
    }


def test_the_placeholder_is_not_a_valid_name_itself():
    # So it can never be mistaken for (or collide with) a real object.
    for pattern in (s._DNS_LABEL, s._DNS_SUBDOMAIN, s._OBJECT_REF):
        assert not re.fullmatch(pattern, s.INVALID_K8S_NAME)
    # And sending it is just another bad name: it stays the placeholder.
    item = validated_item("problem_pods", {**POD_ITEM, "name": s.INVALID_K8S_NAME})
    assert item["name"] == s.INVALID_K8S_NAME


def test_pod_counts():
    counts = make_cluster()["pod_counts"]
    assert is_valid(with_cluster(pod_counts={**counts, "total": 5}))
    assert not is_valid(with_cluster(pod_counts={**counts, "extra": 5}))
    assert not is_valid(with_cluster(pod_counts={"total": 1}))
    assert not is_valid(with_cluster(pod_counts={**counts, "total": -1}))


# --- ok_pods ------------------------------------------------------------------------------------


def test_ok_pods_accepts_empty_and_nulls():
    assert is_valid(with_cluster(ok_pods={}))
    assert is_valid(with_cluster(ok_pods={"apps": [["web-1", None, 0, None]]}))


@pytest.mark.parametrize(
    "bad",
    [
        [],
        "x",
        {"apps": "x"},
        {"apps": [["web-1", "n", 0]]},
        {"apps": [["web-1", "n", 0, 5, 6]]},
        {"apps": ["web-1"]},
        {"apps": [[5, "n", 0, 5]]},
        {"apps": [["web-1", "n", "0", 5]]},
        {"apps": [["web-1", "n", True, 5]]},
        {"apps": [["web-1", "n", -1, 5]]},
        {"apps": [["web-1", "n", 0, "5"]]},
    ],
)
def test_ok_pods_rejects_malformed_data(bad):
    assert not is_valid(with_cluster(ok_pods=bad))


def test_ok_pods_caps():
    pod = ["web-1", "worker-1", 0, 5]
    assert is_valid(with_cluster(ok_pods={"a": [pod] * s.K8S_MAX_OK_PODS}))
    assert not is_valid(with_cluster(ok_pods={"a": [pod] * (s.K8S_MAX_OK_PODS + 1)}))
    # 400 pods spread over two namespaces still count together
    half = s.K8S_MAX_OK_PODS // 2
    assert is_valid(with_cluster(ok_pods={"a": [pod] * half, "b": [pod] * half}))
    assert not is_valid(with_cluster(ok_pods={"a": [pod] * half, "b": [pod] * (half + 1)}))
    many = {f"ns{i}": [] for i in range(s.K8S_MAX_OK_NAMESPACES)}
    assert is_valid(with_cluster(ok_pods=many))
    many["one-more"] = []
    assert not is_valid(with_cluster(ok_pods=many))


# --- everything at its cap fits and validates ------------------------------------------------------


def test_worst_case_report_validates_and_fits_the_message_limit():
    from agent.protocol import MAX_REPORT_BYTES, report_message

    cluster = make_cluster(
        nodes=[NODE_ITEM] * s.K8S_MAX_NODES,
        problem_pods=[POD_ITEM] * s.K8S_MAX_PROBLEM_PODS,
        ok_pods={f"ns{i}": [["web-5d9f7c8b6-abcde", "worker-1", 0, 86400]] * 4 for i in range(100)},
        workloads=[WORKLOAD_ITEM] * s.K8S_MAX_WORKLOADS,
        helm=[HELM_ITEM] * s.K8S_MAX_HELM,
        argo=[ARGO_ITEM] * s.K8S_MAX_ARGO,
        pvcs=[PVC_ITEM] * s.K8S_MAX_PVCS,
        events=[EVENT_ITEM] * s.K8S_MAX_EVENTS,
    )
    report = make_kube_report(cluster=cluster)

    assert is_valid(report)
    assert len(json.dumps(report_message(copy.deepcopy(report))).encode()) < MAX_REPORT_BYTES


def test_error_log_lines_name_the_field_but_never_the_data():
    from agents.consumers import error_summary

    report = make_kube_report()
    report["kubernetes"]["cluster"]["pvcs"] = [
        {**PVC_ITEM, "class": 5, "SECRET-key": "SECRET-value"}
    ]
    serializer = check(report)

    lines = " ".join(error_summary(serializer.errors))
    assert "kubernetes.cluster.pvcs" in lines
    assert "SECRET" not in lines
