"""ReportSerializer accepts exactly what agent/report.py builds, and nothing else."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent import collectors
from agent.config import AgentConfig
from agent.report import build_report, command_check
from agents.serializers import (
    MAX_ITEMS,
    HelloSerializer,
    ReportMessageSerializer,
    ReportSerializer,
)

from .sample import SAMPLE_REPORT, make_report

FIXTURES = Path(__file__).resolve().parents[2] / "agent" / "tests" / "fixtures"


def is_valid(report) -> bool:
    return ReportSerializer(data=report).is_valid()


def test_sample_report_is_valid():
    serializer = ReportSerializer(data=SAMPLE_REPORT)

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == SAMPLE_REPORT


def test_real_agent_report_is_valid():
    """Contract test: the agent's own build_report() on fixture output passes."""

    def from_fixture(name, parse):
        text = (FIXTURES / name).read_text(encoding="utf-8")
        return command_check(lambda: text, parse)

    report = build_report(
        AgentConfig(),
        now=datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC),
        collectors={
            "updates": from_fixture("apt_upgradable.txt", collectors.parse_apt_upgradable),
            "reboot": lambda: True,
            "containers": from_fixture("docker_ps.txt", collectors.parse_docker_ps),
            "units": from_fixture("systemctl_failed.txt", collectors.parse_systemctl_failed),
            "timers": from_fixture("systemctl_timers.txt", collectors.parse_systemctl_timers),
            "disks": from_fixture("df.txt", collectors.parse_df),
        },
    )
    serializer = ReportSerializer(data=report)

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == report


def test_agent_report_with_a_crashed_check_is_valid():
    def broken():
        raise OSError

    report = build_report(
        AgentConfig(enabled_checks=["disks"]),
        collectors={"disks": broken},
    )

    assert is_valid(report)
    assert report["errors"] == [{"check": "disks", "error": "OSError"}]


@pytest.mark.parametrize(
    "key", ["updates", "reboot_required", "containers", "failed_units", "timers", "disks"]
)
def test_each_check_may_be_null(key):
    assert is_valid(make_report(**{key: None}))


@pytest.mark.parametrize("key", ["hostname", "agent_version", "timestamp", "errors"])
def test_required_values_may_not_be_null(key):
    assert not is_valid(make_report(**{key: None}))


@pytest.mark.parametrize("key", sorted(SAMPLE_REPORT))
def test_every_key_is_required(key):
    report = make_report()
    del report[key]

    assert not is_valid(report)


# --- unknown fields at every level -------------------------------------------


def test_unknown_top_level_field():
    assert not is_valid(make_report(extra=1))


def test_unknown_field_in_updates():
    assert not is_valid(make_report(updates={"total": 1, "security": 0, "kernel": 1}))


def test_unknown_field_in_container():
    report = make_report()
    report["containers"][0]["env"] = {"PASSWORD": "x"}

    assert not is_valid(report)


def test_unknown_field_in_timer_disk_and_error():
    for key, item in [
        ("timers", {**SAMPLE_REPORT["timers"][0], "x": 1}),
        ("disks", {"mount": "/", "percent": 1, "x": 1}),
        ("errors", {"check": "disks", "error": "OSError", "message": "secret path"}),
    ]:
        assert not is_valid(make_report(**{key: [item]})), key


def test_unknown_field_in_message_envelopes():
    hello = {"type": "hello", "agent_version": "0.1.0", "hostname": "nas", "os": "Ubuntu"}
    assert HelloSerializer(data=hello).is_valid()
    assert not HelloSerializer(data={**hello, "cmd": "rm -rf /"}).is_valid()

    message = {"type": "report", "report": make_report()}
    assert ReportMessageSerializer(data=message).is_valid()
    assert not ReportMessageSerializer(data={**message, "extra": 1}).is_valid()


# --- exact types ---------------------------------------------------------------


@pytest.mark.parametrize("value", ["3", 3.0, True, None, [3]])
def test_update_counts_must_be_integers(value):
    assert not is_valid(make_report(updates={"total": value, "security": 0}))


@pytest.mark.parametrize("value", [1, 0, "true", "yes"])
def test_reboot_required_must_be_a_boolean(value):
    assert not is_valid(make_report(reboot_required=value))


@pytest.mark.parametrize("value", [42.5, "42", True])
def test_disk_percent_must_be_an_integer(value):
    assert not is_valid(make_report(disks=[{"mount": "/", "percent": value}]))


@pytest.mark.parametrize("value", [123, ["nas"], {"x": 1}, True])
def test_strings_must_be_strings(value):
    assert not is_valid(make_report(hostname=value))


@pytest.mark.parametrize(
    "field,value",
    [
        ("health", "sick"),
        ("health", True),
        ("exit_code", "137"),
        ("exit_code", 1.0),
        ("name", 5),
    ],
)
def test_container_field_types(field, value):
    report = make_report()
    report["containers"][0][field] = value

    assert not is_valid(report)


@pytest.mark.parametrize("value", [{"total": 1}, "updates", [1, 2]])
def test_sections_must_have_the_right_shape(value):
    assert not is_valid(make_report(updates=value))


@pytest.mark.parametrize("key", ["containers", "failed_units", "timers", "disks", "errors"])
def test_lists_must_be_lists(key):
    assert not is_valid(make_report(**{key: {"a": 1}}))


def test_whole_report_must_be_an_object():
    assert not is_valid([SAMPLE_REPORT])
    assert not is_valid("report")


# --- sizes and values --------------------------------------------------------------


def test_list_size_limit():
    disks = [{"mount": f"/m{i}", "percent": 1} for i in range(MAX_ITEMS + 1)]

    assert is_valid(make_report(disks=disks[:MAX_ITEMS]))
    assert not is_valid(make_report(disks=disks))
    assert not is_valid(make_report(failed_units=["x.service"] * (MAX_ITEMS + 1)))


def test_string_length_limit():
    report = make_report()
    report["containers"][0]["name"] = "a" * 191

    assert not is_valid(report)
    assert not is_valid(make_report(hostname="h" * 254))


@pytest.mark.parametrize("value", ["nginx\n[fake log line]", "a\x00b", "esc\x1b[31m"])
def test_control_characters_rejected(value):
    assert not is_valid(make_report(failed_units=[value]))


def test_strings_are_kept_exactly():
    report = make_report(failed_units=[" padded.service "])
    serializer = ReportSerializer(data=report)

    assert serializer.is_valid()
    assert serializer.validated_data["failed_units"] == [" padded.service "]


@pytest.mark.parametrize("percent", [-1, 101])
def test_disk_percent_range(percent):
    assert not is_valid(make_report(disks=[{"mount": "/", "percent": percent}]))


def test_negative_update_count():
    assert not is_valid(make_report(updates={"total": -1, "security": 0}))


def test_more_security_updates_than_total():
    assert not is_valid(make_report(updates={"total": 1, "security": 2}))


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-12 12:00:00",
        "2026-10-12T12:00:00+00:00",
        "2026-10-12T12:00:00.123Z",
        "2026-13-45T12:00:00Z",
        "yesterday",
    ],
)
def test_bad_timestamps(value):
    assert not is_valid(make_report(timestamp=value))
