import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent import AGENT_VERSION, collectors
from agent.config import AgentConfig
from agent.report import (
    DEFAULT_COLLECTORS,
    REPORT_KEYS,
    Collector,
    build_report,
    command_check,
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
