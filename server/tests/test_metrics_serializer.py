"""The report's `metrics` key (agent 0.4.0): strictly validated, and never fatal.

A metrics part that fails validation becomes null, so a broken or hostile value can't hide
the host's health checks; everything else about the report is still validated as before.
"""

import copy
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.metrics import MetricsSampler
from agents.serializers import (
    METRICS_MAX_CONTAINERS,
    METRICS_MAX_MOUNTS,
    MetricsSerializer,
    ReportSerializer,
)

from .sample import SAMPLE_METRICS, make_metrics, make_metrics_report, make_report

AGENT_FIXTURES = Path(__file__).resolve().parents[2] / "agent" / "tests" / "fixtures" / "metrics"


def metrics_ok(data) -> bool:
    return MetricsSerializer(data=data).is_valid()


def validated(report):
    serializer = ReportSerializer(data=report)
    assert serializer.is_valid(), serializer.errors
    return serializer.validated_data


def test_sample_metrics_are_valid_and_unchanged():
    serializer = MetricsSerializer(data=SAMPLE_METRICS)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == SAMPLE_METRICS


def test_the_agents_own_metrics_pass(tmp_path):
    """Contract test: agent/metrics.py on fixture /proc files passes the serializer."""
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    for name, target in (
        ("proc_stat_1.txt", "stat"),
        ("loadavg.txt", "loadavg"),
        ("meminfo.txt", "meminfo"),
        ("diskstats_1.txt", "diskstats"),
        ("mountinfo.txt", "self/mountinfo"),
    ):
        shutil.copy(AGENT_FIXTURES / name, proc / target)
    clock = SimpleNamespace(now=100.0)
    sampler = MetricsSampler(
        proc_root=str(proc),
        thermal_root=str(tmp_path / "none"),
        statvfs=lambda path: SimpleNamespace(
            f_blocks=1000, f_bfree=400, f_bavail=350, f_files=100, f_ffree=75, f_frsize=4096
        ),
        clock=lambda: clock.now,
        docker_socket=str(tmp_path / "none.sock"),
    )
    first = sampler.collect()
    shutil.copy(AGENT_FIXTURES / "proc_stat_2.txt", proc / "stat")
    shutil.copy(AGENT_FIXTURES / "diskstats_2.txt", proc / "diskstats")
    clock.now += 60
    second = sampler.collect()
    for data in (first, second):
        serializer = MetricsSerializer(data=data)
        assert serializer.is_valid(), serializer.errors


def test_old_reports_without_metrics_are_unchanged():
    assert "metrics" not in validated(make_report())


def test_metrics_null_is_kept():
    assert validated(make_metrics_report(None))["metrics"] is None


def test_valid_metrics_are_kept():
    assert validated(make_metrics_report())["metrics"] == SAMPLE_METRICS


# --- junk never breaks the report --------------------------------------------------------


def with_change(path: list, value):
    data = copy.deepcopy(SAMPLE_METRICS)
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return data


HOSTILE = [
    (["cpu", "busy_pct"], 101),
    (["cpu", "busy_pct"], -1),
    (["cpu", "busy_pct"], "50"),
    (["cpu", "busy_pct"], True),
    (["cpu", "busy_pct"], float("nan")),
    (["cpu", "busy_pct"], float("inf")),
    (["cpu", "load"], [1, 2]),
    (["cpu", "load"], [1, 2, float("nan")]),
    (["cpu", "cores"], 0),
    (["cpu", "temp_c"], 900),
    (["memory", "available"], 9 * 1024**3),  # more than total
    (["memory", "swap_used"], 3 * 1024**3),  # more than swap_total
    (["memory", "total"], 2**61),
    (["mounts", 0, "mount"], "relative/path"),
    (["mounts", 0, "mount"], "/evil\npath"),
    (["mounts", 0, "mount"], "/" + "x" * 300),
    (["mounts", 0, "fs"], "EXT4; rm"),
    (["mounts", 0, "free"], 10**15),  # more than its size
    (["mounts", 0, "used_pct"], None),
    (["containers", 0, "name"], ""),
    (["containers", 0, "name"], "bad\x1bname"),
    (["containers", 0, "name"], "--all"),  # would read as a flag in the copy-only fix
    (["containers", 0, "name"], "a b; rm -rf ~"),
    (["containers", 0, "mem_limit"], 0),
    (["containers", 0, "rx_bps"], -5),
    (["containers", 0, "cpu_pct"], 400.0),  # docker-stats style per-core sums are refused
    (["containers_truncated"], "no"),
    (["interval_s"], -1),
]


@pytest.mark.parametrize(("path", "value"), HOSTILE, ids=lambda v: str(v)[:30])
def test_hostile_values_are_refused_by_the_metrics_serializer(path, value):
    assert not metrics_ok(with_change(path, value))


@pytest.mark.parametrize(("path", "value"), HOSTILE, ids=lambda v: str(v)[:30])
def test_hostile_metrics_become_null_and_the_report_is_still_saved(path, value):
    report = make_metrics_report(with_change(path, value), updates={"total": 3, "security": 1})
    data = validated(report)
    assert data["metrics"] is None
    assert data["updates"] == {"total": 3, "security": 1}


@pytest.mark.parametrize(
    "junk",
    ["metrics", 42, [], {}, {"cpu": {}}, {**SAMPLE_METRICS, "extra": 1}],
    ids=["string", "number", "list", "empty", "partial", "unknown key"],
)
def test_wrong_shapes_become_null(junk):
    assert validated(make_metrics_report(junk))["metrics"] is None


def test_too_many_mounts_or_containers():
    mount = SAMPLE_METRICS["mounts"][0]
    mounts = [{**mount, "mount": f"/m{i}"} for i in range(METRICS_MAX_MOUNTS + 1)]
    assert not metrics_ok(make_metrics(mounts=mounts))
    container = SAMPLE_METRICS["containers"][0]
    many = [{**container, "name": f"c{i}"} for i in range(METRICS_MAX_CONTAINERS + 1)]
    assert not metrics_ok(make_metrics(containers=many))
    assert metrics_ok(make_metrics(containers=many[:METRICS_MAX_CONTAINERS]))


def test_duplicate_names_are_refused():
    container = SAMPLE_METRICS["containers"][0]
    assert not metrics_ok(make_metrics(containers=[container, container]))
    mount = SAMPLE_METRICS["mounts"][0]
    assert not metrics_ok(make_metrics(mounts=[mount, mount]))


def test_the_rest_of_the_report_is_still_strict():
    # A lenient metrics key must not make the report lenient: junk elsewhere still fails.
    assert not ReportSerializer(data=make_metrics_report(updates={"total": "3"})).is_valid()


def test_the_log_names_the_field_never_the_value(caplog):
    secret = "SENTINEL-METRICS-VALUE"
    data = with_change(["containers", 0, "name"], f"{secret}\x07")
    data["containers"][0]["unknown_key_" + secret] = 1
    with caplog.at_level(logging.WARNING, logger="agents.serializers"):
        assert validated(make_metrics_report(data))["metrics"] is None
    assert "Ignored an invalid metrics part" in caplog.text
    assert "containers[0]" in caplog.text
    assert secret not in caplog.text
