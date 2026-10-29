import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent import AGENT_VERSION, collectors, report
from agent.config import AgentConfig, KubernetesConfig
from agent.protocol import MAX_REPORT_BYTES, report_message
from agent.report import (
    DEFAULT_COLLECTORS,
    REPORT_KEYS,
    Collector,
    build_report,
    command_check,
    fit_report,
    safe_collect,
)

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)

EXPECTED_KEYS = {
    "hostname",
    "agent_version",
    "timestamp",
    "updates",
    "reboot_required",
    "containers",
    "failed_units",
    "timers",
    "disks",
    "errors",
}


def fake_collectors(**overrides: Collector) -> dict[str, Collector]:
    collectors: dict[str, Collector] = {
        "updates": lambda: {"total": 3, "security": 1},
        "reboot": lambda: False,
        "containers": lambda: [
            {
                "name": "nginx",
                "state": "running",
                "status": "Up 2 hours",
                "health": None,
                "exit_code": None,
            }
        ],
        "units": list,
        "timers": list,
        "disks": lambda: [{"mount": "/", "percent": 42}],
    }
    collectors.update(overrides)
    return collectors


def failing_collector(*_args: Any) -> Any:
    raise AssertionError("this collector must not run")


def test_exact_key_set() -> None:
    report = build_report(AgentConfig(), now=NOW, collectors=fake_collectors())

    assert set(report) == EXPECTED_KEYS
    assert report["agent_version"] == AGENT_VERSION
    assert isinstance(report["hostname"], str)
    assert report["updates"] == {"total": 3, "security": 1}
    assert report["reboot_required"] is False
    assert report["errors"] == []


def test_report_is_json_serializable() -> None:
    report = build_report(AgentConfig(), now=NOW, collectors=fake_collectors())

    assert json.loads(json.dumps(report)) == report


def test_default_collectors_cover_every_check() -> None:
    assert set(DEFAULT_COLLECTORS) == set(REPORT_KEYS)


def test_disabled_check_is_null_and_not_run() -> None:
    config = AgentConfig(enabled_checks=["updates"])
    collectors = fake_collectors(
        reboot=failing_collector,
        containers=failing_collector,
        units=failing_collector,
        timers=failing_collector,
        disks=failing_collector,
    )

    report = build_report(config, now=NOW, collectors=collectors)

    assert report["updates"] == {"total": 3, "security": 1}
    for key in ("reboot_required", "containers", "failed_units", "timers", "disks"):
        assert report[key] is None
    assert report["errors"] == []


def test_missing_tool_is_null_without_error() -> None:
    # run_* returns None when the command isn't installed; parse must not run.
    missing_docker = command_check(lambda: None, failing_collector)

    report = build_report(
        AgentConfig(), now=NOW, collectors=fake_collectors(containers=missing_docker)
    )

    assert report["containers"] is None
    assert report["errors"] == []


def test_command_check_parses_output() -> None:
    collect = command_check(lambda: "raw text", lambda text: text.upper())

    assert collect() == "RAW TEXT"


def test_raising_collector_is_null_with_error_entry() -> None:
    def broken() -> Any:
        raise PermissionError("[Errno 13] Permission denied: '/home/dimi/secret-path'")

    report = build_report(AgentConfig(), now=NOW, collectors=fake_collectors(disks=broken))

    assert report["disks"] is None
    assert report["errors"] == [{"check": "disks", "error": "PermissionError"}]
    # No message text or path leaks anywhere in the report.
    assert "secret-path" not in json.dumps(report)
    assert "Permission denied" not in json.dumps(report)
    # The other checks still ran.
    assert report["updates"] == {"total": 3, "security": 1}


def test_several_failures_each_get_an_entry() -> None:
    def broken() -> Any:
        raise ValueError("boom")

    report = build_report(
        AgentConfig(), now=NOW, collectors=fake_collectors(updates=broken, timers=broken)
    )

    assert report["errors"] == [
        {"check": "updates", "error": "ValueError"},
        {"check": "timers", "error": "ValueError"},
    ]


def test_safe_collect() -> None:
    assert safe_collect("disks", lambda: [1]) == ([1], None)
    assert safe_collect("disks", lambda: 1 / 0) == (
        None,
        {"check": "disks", "error": "ZeroDivisionError"},
    )


def test_timestamp_format() -> None:
    report = build_report(AgentConfig(), now=NOW, collectors=fake_collectors())

    assert report["timestamp"] == "2026-10-12T12:00:00Z"


def test_timestamp_is_converted_to_utc() -> None:
    skopje_summer = timezone(timedelta(hours=2))
    now = datetime(2026, 10, 12, 14, 0, 0, 123456, tzinfo=skopje_summer)

    report = build_report(AgentConfig(), now=now, collectors=fake_collectors())

    assert report["timestamp"] == "2026-10-12T12:00:00Z"


def test_timestamp_defaults_to_now() -> None:
    report = build_report(AgentConfig(), collectors=fake_collectors())

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", report["timestamp"])


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError):
        build_report(AgentConfig(), now=datetime(2026, 10, 12), collectors=fake_collectors())


def test_report_items_match_the_contract() -> None:
    """Fixtures through the real parsers: every item has exactly the keys in guide section 6."""
    fixtures = Path(__file__).parent / "fixtures"

    def from_fixture(name: str, parse: Any) -> Collector:
        text = (fixtures / name).read_text(encoding="utf-8")
        return command_check(lambda: text, parse)

    report = build_report(
        AgentConfig(),
        now=NOW,
        collectors={
            "updates": from_fixture("apt_upgradable.txt", collectors.parse_apt_upgradable),
            "reboot": lambda: True,
            "containers": from_fixture("docker_ps.txt", collectors.parse_docker_ps),
            "units": from_fixture("systemctl_failed.txt", collectors.parse_systemctl_failed),
            "timers": from_fixture("systemctl_timers.txt", collectors.parse_systemctl_timers),
            "disks": from_fixture("df.txt", collectors.parse_df),
        },
    )

    assert set(report["updates"]) == {"total", "security"}
    assert report["reboot_required"] is True
    assert report["containers"]
    for container in report["containers"]:
        assert set(container) == {"name", "state", "status", "health", "exit_code"}
        assert container["health"] in {"healthy", "unhealthy", "starting", None}
        assert container["exit_code"] is None or isinstance(container["exit_code"], int)
    assert report["failed_units"]
    assert all(isinstance(unit, str) for unit in report["failed_units"])
    assert report["timers"]
    for timer in report["timers"]:
        assert set(timer) == {"timer", "activates", "last_run", "next_run"}
    assert report["disks"]
    for disk in report["disks"]:
        assert set(disk) == {"mount", "percent"}
        assert isinstance(disk["percent"], int)
    assert report["errors"] == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("nas\nfake-line", "nas fake-line"),
        ("a" * 300, "a" * 253),
        ("", "unknown"),
    ],
)
def test_hostname_is_cleaned_like_the_hello_message(monkeypatch, raw, expected) -> None:
    monkeypatch.setattr("agent.report.socket.gethostname", lambda: raw)
    report = build_report(AgentConfig(), now=NOW, collectors=fake_collectors())
    assert report["hostname"] == expected


# --- Kubernetes -------------------------------------------------------------------------------

NODE = {
    "role": "server",
    "dist": "k3s",
    "name": "server-1",
    "k3s": "v1.31.2+k3s1",
    "kubelet": None,
    "helm": "v3.16.2",
}


def kube_config(**kwargs) -> AgentConfig:
    return AgentConfig(kubernetes=KubernetesConfig(enabled=True, **kwargs))


def test_no_kubernetes_key_unless_enabled() -> None:
    collectors = fake_collectors(kubernetes=failing_collector)

    plain = build_report(AgentConfig(), now=NOW, collectors=collectors)
    disabled = build_report(
        AgentConfig(kubernetes=KubernetesConfig(enabled=False, cluster_view=False)),
        now=NOW,
        collectors=collectors,
    )

    assert set(plain) == EXPECTED_KEYS
    assert "kubernetes" not in disabled
    assert plain == disabled


def test_kubernetes_key_when_enabled() -> None:
    value = {"node": NODE, "cluster": None}
    collectors = fake_collectors(kubernetes=lambda: value)

    result = build_report(kube_config(), now=NOW, collectors=collectors)

    assert set(result) == EXPECTED_KEYS | {"kubernetes"}
    assert result["kubernetes"] == value
    assert result["errors"] == []
    assert list(result)[-1] == "errors"  # errors stays last


def test_kubernetes_collector_failure_is_null_with_an_error_entry() -> None:
    def broken() -> Any:
        raise RuntimeError("token abc123 rejected")

    result = build_report(kube_config(), now=NOW, collectors=fake_collectors(kubernetes=broken))

    assert result["kubernetes"] is None
    assert result["errors"] == [{"check": "kubernetes", "error": "RuntimeError"}]
    assert "abc123" not in json.dumps(result)


def test_default_kubernetes_collector_without_cluster_view(monkeypatch) -> None:
    monkeypatch.setattr(collectors, "collect_node_role", lambda: NODE)
    monkeypatch.setattr(
        report, "KubeClient", lambda *a, **k: pytest.fail("no cluster view, no API client")
    )

    result = build_report(kube_config(), now=NOW, collectors=fake_collectors())

    assert result["kubernetes"] == {"node": NODE, "cluster": None}


def test_default_kubernetes_collector_with_cluster_view(monkeypatch) -> None:
    seen = {}

    class FakeClient:
        def __init__(self, api_server, token_file, ca_file) -> None:
            seen["args"] = (api_server, token_file, ca_file)

    def fake_view(client, now):
        seen["client"], seen["now"] = client, now
        return {"uid": "u"}

    monkeypatch.setattr(collectors, "collect_node_role", lambda: NODE)
    monkeypatch.setattr(report, "KubeClient", FakeClient)
    monkeypatch.setattr(report, "collect_cluster_view", fake_view)
    config = kube_config(
        cluster_view=True,
        api_server="https://10.0.0.5:6443",
        token_file="/etc/cronrack/k8s-token",
        ca_file="/etc/cronrack/k8s-ca.crt",
    )

    result = build_report(config, now=NOW, collectors=fake_collectors())

    assert result["kubernetes"] == {"node": NODE, "cluster": {"uid": "u"}}
    assert seen["args"] == (
        "https://10.0.0.5:6443",
        "/etc/cronrack/k8s-token",
        "/etc/cronrack/k8s-ca.crt",
    )
    assert seen["now"] == NOW


# --- fit_report -------------------------------------------------------------------------------


def size(rep: dict) -> int:
    return len(json.dumps(report_message(rep)).encode())


def pod(i: int, text: str = "p") -> dict:
    return {
        "ns": f"ns{i % 20}",
        "name": f"{text}-{i:04d}",
        "node": "worker-1",
        "phase": "Running",
        "ready": [0, 1],
        "restarts": 12,
        "reason": "CrashLoopBackOff",
        "last": "OOMKilled",
        "last_age_s": 120,
        "age_s": 86400,
        "owner": f"Deployment/{text}",
    }


def workload(i: int, healthy: bool, text: str = "w") -> dict:
    return {
        "kind": "Deployment",
        "ns": f"ns{i % 20}",
        "name": f"{text}-{i:04d}",
        "desired": 2,
        "ready": 2 if healthy else 0,
        "available": 2 if healthy else 0,
        "updated": 2,
        "misscheduled": 0,
        "images": [f"ghcr.io/example/{text}{i}:1.0"] * 3,
        "helm": None,
    }


def cluster(text: str = "x", events_text: str = "m") -> dict:
    return {
        "uid": "11111111-2222-3333-4444-555555555555",
        "version": "v1.31.2+k3s1",
        "api": {"error": None, "cert_days": 290},
        "sa": "system:serviceaccount:monitoring:cronrack-agent",
        "token_days": 300,
        "denied": [{"g": "", "r": "pods", "v": "list"}],
        "missing": ["argoproj.io"],
        "nodes": [{"name": f"n{i}", "roles": [], "ready": True, "pressure": []} for i in range(50)],
        "problem_pods": [pod(i, text) for i in range(100)],
        "ok_pods": {f"ns{i % 20}": [[f"{text}-{i:04d}", "worker-1", 0, 86400]] for i in range(400)},
        "pod_counts": {
            "total": 500,
            "running": 400,
            "pending": 0,
            "succeeded": 0,
            "failed": 0,
            "problem": 100,
        },
        "workloads": [workload(i, healthy=i % 2 == 0, text=text) for i in range(200)],
        "helm": [
            {"name": f"h{i}", "ns": "n", "chart": "c-1", "app_version": "1", "source": "labels"}
            for i in range(100)
        ],
        "argo": [
            {
                "name": f"a{i}",
                "ns": "argocd",
                "project": "default",
                "sync": "Synced",
                "health": "Healthy",
                "rev": "abc1234",
                "op": None,
                "synced_at": None,
            }
            for i in range(100)
        ],
        "argocd_version": "v2.13.1",
        "pvcs": [
            {
                "ns": "n",
                "name": f"c{i}",
                "phase": "Bound",
                "class": "local-path",
                "size": "1Gi",
                "age_s": 5,
            }
            for i in range(100)
        ],
        "events": [
            {
                "ns": "n",
                "obj": f"Pod/{text}{i}",
                "reason": "BackOff",
                "msg": events_text * 256,
                "count": 3,
                "age_s": 5,
            }
            for i in range(20)
        ],
        "truncated": False,
        "cut": [],
    }


def host_report(cluster_part: dict | None, **extra) -> dict:
    rep = build_report(AgentConfig(), now=NOW, collectors=fake_collectors(**extra))
    rep["kubernetes"] = {"node": NODE, "cluster": cluster_part}
    return rep


def test_fit_report_leaves_a_small_report_alone() -> None:
    rep = host_report(cluster())
    assert size(rep) < MAX_REPORT_BYTES

    assert fit_report(rep, MAX_REPORT_BYTES) == rep


def test_fit_report_without_kubernetes_changes_nothing() -> None:
    rep = build_report(AgentConfig(), now=NOW, collectors=fake_collectors())
    assert fit_report(rep, 10) == rep  # nothing it is allowed to cut


def test_fit_report_does_not_modify_its_input() -> None:
    rep = host_report(cluster())
    before = json.dumps(rep)

    fit_report(rep, 5_000)

    assert json.dumps(rep) == before


def test_fit_report_first_drops_healthy_pods() -> None:
    rep = host_report(cluster())
    limit = size(rep) - 1

    fitted = fit_report(rep, limit)

    view = fitted["kubernetes"]["cluster"]
    assert view["ok_pods"] == {}
    assert view["truncated"] is True
    assert view["cut"] == ["ok_pods"]
    assert len(view["problem_pods"]) == 100
    assert len(view["workloads"]) == 200
    assert len(view["events"]) == 20
    assert size(fitted) <= limit


def test_fit_report_then_drops_healthy_workloads_then_events() -> None:
    base = cluster()
    no_ok = host_report({**base, "ok_pods": {}, "cut": ["ok_pods"], "truncated": True})
    step2 = fit_report(no_ok, size(no_ok) - 1)["kubernetes"]["cluster"]

    assert step2["cut"] == ["ok_pods", "workloads"]
    assert len(step2["workloads"]) == 100
    assert all(w["available"] < w["desired"] for w in step2["workloads"])
    assert len(step2["events"]) == 20

    rep3 = host_report(step2)
    step3 = fit_report(rep3, size(rep3) - 1)["kubernetes"]["cluster"]

    assert step3["cut"] == ["ok_pods", "workloads", "events"]
    assert step3["events"] == []
    assert len(step3["workloads"]) == 100


def test_fit_report_keeps_problems_nodes_and_denied_to_the_end() -> None:
    rep = host_report(cluster())

    fitted = fit_report(rep, 40_000)

    view = fitted["kubernetes"]["cluster"]
    assert size(fitted) <= 40_000
    assert view["workloads"] == [] and view["helm"] == [] and view["argo"] == []
    assert view["pvcs"] == []
    assert len(view["problem_pods"]) == 100
    assert len(view["nodes"]) == 50
    assert view["denied"] == [{"g": "", "r": "pods", "v": "list"}]
    assert view["truncated"] is True
    assert {"ok_pods", "workloads", "events", "helm", "argo", "pvcs"} <= set(view["cut"])
    assert view["pod_counts"]["total"] == 500  # counts survive every cut


def test_fit_report_last_resort_keeps_identity_and_errors_only() -> None:
    rep = host_report(cluster())

    fitted = fit_report(rep, 3_000)

    view = fitted["kubernetes"]["cluster"]
    assert view["uid"] == "11111111-2222-3333-4444-555555555555"
    assert view["api"] == {"error": None, "cert_days": 290}
    assert view["nodes"] == [] and view["problem_pods"] == []
    assert view["truncated"] is True
    assert fitted["kubernetes"]["node"] == NODE


def test_fit_report_worst_case_unicode_stays_under_the_budget() -> None:
    # Every list at its cap, with non-ASCII text that json.dumps writes as 6-12 byte escapes.
    rep = host_report(cluster(text="é" * 30, events_text="\U0001f600"))
    assert size(rep) > MAX_REPORT_BYTES

    fitted = fit_report(rep, MAX_REPORT_BYTES)

    assert size(fitted) <= MAX_REPORT_BYTES
    assert fitted["kubernetes"]["cluster"]["truncated"] is True


def test_fit_report_result_is_still_json() -> None:
    fitted = fit_report(host_report(cluster()), 5_000)
    assert json.loads(json.dumps(fitted)) == fitted
